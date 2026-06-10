"""The real API surface — exactly the endpoints Meta is releasing, in
360dialog convention (no phone_number_id in paths; D360-API-KEY header).
Behavior per v1 §5–§9."""
from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app import ids, rules, webhooks
from app.deps import Ctx, auth
from app.errors import ApiError
from app.models import Bsuid, Message, Template, UsernameChange

router = APIRouter(tags=["Messages"])
ERR_EXAMPLE = {"error": {"message": "(#131047) Re-engagement message",
                         "type": "OAuthException", "code": 131047,
                         "error_data": {"messaging_product": "whatsapp",
                                        "details": "Message outside the 24-hour service window. "
                                                   "Use an approved template."},
                         "fbtrace_id": "A21bd..."}}


# ------------------------------------------------------------- POST /messages

async def _find_template(ctx: Ctx, body: dict) -> Template:
    tpl_req = body.get("template") or {}
    name = tpl_req.get("name")
    if not name:
        raise ApiError(100, "template.name is required for template sends.")
    q = select(Template).where(Template.api_key_id == ctx.key.id, Template.name == name)
    lang = (tpl_req.get("language") or {}).get("code")
    if lang:
        q = q.where(Template.language == lang)
    tpl = (await ctx.session.execute(q)).scalars().first()
    if tpl is None:
        raise ApiError(132001, f"Template '{name}' does not exist for this number.", 404)
    return tpl


async def _send(ctx: Ctx, body: dict, *, marketing: bool) -> dict:
    session, key = ctx.session, ctx.key
    cfg = rules.effective_config(key)
    await rules.pop_injected_error(session, key,
                                   "marketing_messages" if marketing else "messages")

    to, recipient = body.get("to"), body.get("recipient")
    if not to and not recipient:
        raise ApiError(100, "Provide 'to' (phone) or 'recipient' (BSUID).")
    addressed_by = "phone" if to else "bsuid"  # v1 §8.1.1: 'to' wins when both present

    mtype = body.get("type", "text")
    if mtype not in ("text", "template", "interactive"):
        raise ApiError(100, f"Message type '{mtype}' is not implemented in the sandbox. "
                            "Supported: text, template, interactive.")

    template = None
    category = "service"
    if mtype == "template":
        template = await _find_template(ctx, body)
        category = "marketing" if template.category == "marketing" else "utility"
    if marketing:
        if mtype != "template":
            raise ApiError(100, "/marketing_messages only accepts type=template.")
        if template.category != "marketing":
            raise ApiError(100, f"Template '{template.name}' is category "
                                f"'{template.category}'; /marketing_messages requires "
                                "a marketing template.")

    if mtype == "interactive":
        itype = (body.get("interactive") or {}).get("type")
        if itype == "contact_request":
            raise ApiError(100, "'contact_request' is not a valid interactive type. "
                                "Use 'request_contact_info'.")
        if itype != "request_contact_info":
            raise ApiError(100, f"interactive.type '{itype}' is not implemented in the "
                                "sandbox. Use 'request_contact_info'.")

    phone_changed = False
    old_bsuid = key.user.bsuid
    if addressed_by == "phone":
        user, phone_changed = await rules.resolve_phone(session, key, str(to))
    else:
        user = await rules.resolve_bsuid(session, key, cfg, str(recipient))

    # v1 §8.1.4: auth-flavored templates cannot target BSUIDs
    if template is not None and template.auth_flavor and addressed_by == "bsuid":
        raise ApiError(131062, "Business-scoped User ID (BSUID) recipients are not "
                               "supported for this message.")

    # v1 §8.1.5: free-form sends require an open 24h service window
    if mtype != "template" and not rules.window_open(key, cfg):
        raise ApiError(131047, "Message outside the 24-hour service window. Use an "
                               "approved template, or set user.service_window='open' / "
                               "consumer_actions.reply_to_messages=true in "
                               "PUT /sandbox/config.")

    when = rules.now()
    wamid = await ids.new_wamid(session)
    payload = {k: v for k, v in body.items() if k != "messaging_product"}
    payload["_category"] = category
    msg = Message(id=await ids.new_id(session, "msg"), wamid=wamid, direction="outbound",
                  api_key_id=key.id, addressed_by=addressed_by, type=mtype,
                  payload=payload, statuses=[], created_at=when)
    session.add(msg)
    await session.flush()

    if phone_changed:
        await webhooks.emit_system_phone_change(session, key, old_bsuid, user.bsuid)
    if addressed_by == "phone":
        # v1 §6: phone traffic writes contact book + cache immediately;
        # BSUID sends write only on delivery (phone_known=False).
        rules.touch_contact(user, when, phone_known=True)
    await session.commit()
    webhooks.schedule_message_lifecycle(msg.id, cfg)

    message_obj: dict = {"id": wamid}
    if marketing:
        message_obj["message_status"] = "accepted"
    if addressed_by == "phone":
        contact = {"input": str(to), "wa_id": user.phone}
    else:
        contact = {"input": str(recipient),
                   "user_id": user.parent_bsuid
                   if rules.PARENT_BSUID_RE.match(str(recipient)) else user.bsuid}
    return {"messaging_product": "whatsapp", "contacts": [contact],
            "messages": [message_obj]}


@router.post(
    "/messages",
    summary="Send a message (phone or BSUID recipient)",
    description=(
        "Send `text`, `template` or `interactive` messages.\n\n"
        "**Addressing**: `to` (phone) or `recipient` (BSUID / parent BSUID). When "
        "both are present, `to` wins. Phone sends respond with "
        "`contacts[0].wa_id`; BSUID sends with `contacts[0].user_id` — never both. "
        "A successful send always returns a wamid.\n\n"
        "**Errors**:\n"
        "- `100` — missing recipient, unsupported type, `contact_request` typo\n"
        "- `131009` — malformed, foreign or retired BSUID\n"
        "- `131047` — free-form send outside the 24h service window\n"
        "- `131062` — auth-flavored template addressed to a BSUID\n"
        "- `132001` — unknown template name"
    ),
    openapi_extra={"requestBody": {"content": {"application/json": {"examples": {
        "text to BSUID": {"value": {"recipient": "BR.13491208655302741918",
                                    "type": "text", "text": {"body": "Hello!"}}},
        "text to phone": {"value": {"to": "5511988880001", "type": "text",
                                    "text": {"body": "Hello!"}}},
        "template": {"value": {"to": "5511988880001", "type": "template",
                               "template": {"name": "my_template",
                                            "language": {"code": "en"}}}},
        "request contact info": {"value": {
            "recipient": "BR.13491208655302741918", "type": "interactive",
            "interactive": {"type": "request_contact_info",
                            "body": {"text": "Please share your number"},
                            "action": {"name": "request_contact_info"}}}}}}}}},
    responses={200: {"content": {"application/json": {"examples": {
        "BSUID send": {"value": {"messaging_product": "whatsapp",
                                 "contacts": [{"input": "BR.13491208655302741918",
                                               "user_id": "BR.13491208655302741918"}],
                                 "messages": [{"id": "wamid.HBg..."}]}},
        "phone send": {"value": {"messaging_product": "whatsapp",
                                 "contacts": [{"input": "5511988880001",
                                               "wa_id": "5511988880001"}],
                                 "messages": [{"id": "wamid.HBg..."}]}}}}}},
               400: {"content": {"application/json": {"example": ERR_EXAMPLE}}}})
async def post_messages(body: dict, ctx: Ctx = Depends(auth)):
    return await _send(ctx, body, marketing=False)


@router.post(
    "/marketing_messages",
    summary="Send a marketing template message",
    description=(
        "Same request/response/behavior as `/messages`, but only accepts "
        "`type=template` with a **marketing**-category template, and the "
        "response's `messages[0]` additionally includes "
        "`\"message_status\": \"accepted\"`.\n\n"
        "**Errors**: `100` (non-template type or non-marketing template) plus "
        "everything `/messages` can return."
    ),
    responses={200: {"content": {"application/json": {"example": {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "5511988880001", "wa_id": "5511988880001"}],
        "messages": [{"id": "wamid.HBg...", "message_status": "accepted"}]}}}},
               400: {"content": {"application/json": {"example": ERR_EXAMPLE}}}})
async def post_marketing_messages(body: dict, ctx: Ctx = Depends(auth)):
    return await _send(ctx, body, marketing=True)


# ------------------------------------------------------- username management

@router.get(
    "/username_suggestions", tags=["Business username"],
    summary="Get 3 available username suggestions",
    description="Three plausible names derived from the key's name.",
    responses={200: {"content": {"application/json": {"example": {
        "data": [{"username_suggestions": ["acme", "acme.official", "acme742"]}]}}}}})
async def username_suggestions(ctx: Ctx = Depends(auth)):
    base = "".join(c for c in ctx.key.name.lower() if c.isalnum()) or "business"
    base = base[:20]
    return {"data": [{"username_suggestions": [
        base, f"{base}.official", f"{base}{ids.digits(3, 'sugg', ctx.key.id)}"]}]}


@router.post(
    "/username", tags=["Business username"],
    summary="Claim/change the business username",
    description=(
        "Claims a username for this business number. Status is `approved` when "
        "`ga_mode=true`, else `reserved`. Emits a `business_username_update` "
        "webhook.\n\n**Errors**:\n"
        "- `100` — format violation (3–35 chars of `a-z0-9._`, ≥1 letter, no "
        "leading/trailing/double `.`, not starting `www`, no domain suffix)\n"
        "- `147001` — username taken (case-insensitive, `.`/`_` significant)\n"
        "- `131056` (HTTP **429**) — rate limit: max 3 changes per number per "
        "rolling 14 days; details include the next-available timestamp. Never "
        "returned as 'already claimed'."
    ),
    openapi_extra={"requestBody": {"content": {"application/json": {
        "example": {"username": "acme.support"}}}}},
    responses={200: {"content": {"application/json": {"example": {"status": "approved"}}}},
               429: {"description": "Username change rate limit",
                     "content": {"application/json": {"example": {"error": {
                         "message": "(#131056) Username change rate limit hit",
                         "type": "OAuthException", "code": 131056,
                         "error_data": {"messaging_product": "whatsapp",
                                        "details": "Username change limit reached: max 3 "
                                                   "changes per 14 days. Next change "
                                                   "available at 2026-06-23T00:00:00Z."},
                         "fbtrace_id": "A21bd..."}}}}}})
async def claim_username(body: dict, ctx: Ctx = Depends(auth)):
    session, key = ctx.session, ctx.key
    await rules.pop_injected_error(session, key, "username")
    name = body.get("username")
    rules.validate_username_format(name)

    # rate limit BEFORE uniqueness: a rate-limited claim must yield the
    # dedicated 429, never "already claimed"
    when = rules.now()
    cutoff = when - timedelta(days=14)
    changes = (await session.execute(select(UsernameChange).where(
        UsernameChange.api_key_id == key.id,
        UsernameChange.changed_at > cutoff).order_by(UsernameChange.changed_at))).scalars().all()
    if len(changes) >= 3:
        next_at = (changes[0].changed_at + timedelta(days=14)).isoformat() + "Z"
        raise ApiError(131056, "Username change limit reached: max 3 changes per 14 days. "
                               f"Next change available at {next_at}.", http_status=429)

    await rules.assert_username_available(session, name, exclude_key=key.id)
    key.username = name
    key.username_status = "approved" if rules.effective_config(key)["ga_mode"] else "reserved"
    session.add(UsernameChange(id=await ids.new_id(session, "uc"),
                               api_key_id=key.id, changed_at=when))
    status = key.username_status
    await webhooks.emit_business_username_update(session, key, status)
    await session.commit()
    return {"status": status}


@router.get(
    "/username", tags=["Business username"],
    summary="Read the claimed username",
    description="Returns `{}` when no username is claimed. Status becomes "
                "`active` when `ga_mode=true`.",
    responses={200: {"content": {"application/json": {"example": {
        "username": "acme.support", "status": "active"}}}}})
async def get_username(ctx: Ctx = Depends(auth)):
    if not ctx.key.username:
        return {}
    ga = rules.effective_config(ctx.key)["ga_mode"]
    return {"username": ctx.key.username,
            "status": "active" if ga else ctx.key.username_status}


@router.delete(
    "/username", tags=["Business username"],
    summary="Delete the claimed username",
    description="Returns `success:false` when none was claimed. Emits a "
                "`business_username_update` webhook with status `deleted`.",
    responses={200: {"content": {"application/json": {"example": {"success": True}}}}})
async def delete_username(ctx: Ctx = Depends(auth)):
    had = ctx.key.username is not None
    if had:
        await webhooks.emit_business_username_update(ctx.session, ctx.key, "deleted")
        ctx.key.username = None
        ctx.key.username_status = None
        await ctx.session.commit()
    return {"success": had}


# ------------------------------------------------------ DELETE /contact_book

@router.delete(
    "/contact_book", tags=["Contact book"],
    summary="Delete the contact book entry for a BSUID",
    description=(
        "Removes the portfolio's contact book entry **and clears the 30-day "
        "phone cache** for this consumer. With `ga_mode=true` and a username "
        "adopted, webhooks flip to BSUID-only immediately — but inbound "
        "webhooks keep arriving.\n\n`deleted` is `false` when no entry existed.\n\n"
        "**Errors**: `400` with `success:false` for parent BSUIDs or BSUIDs "
        "that don't belong to this number's portfolio.\n\n"
        "Note: if your sandbox config forces `user.in_contact_book=true`, the "
        "forced state survives this call (set it back to `\"auto\"`)."
    ),
    responses={200: {"content": {"application/json": {"example": {
        "messaging_product": "whatsapp", "success": True, "deleted": True}}}},
               400: {"content": {"application/json": {"example": {
                   "messaging_product": "whatsapp", "success": False, "deleted": False,
                   "details": "Recipient does not exist or does not belong to this "
                              "business portfolio."}}}}})
async def delete_contact_book(bsuid: str, messaging_product: str = "whatsapp",
                              ctx: Ctx = Depends(auth)):
    def reject(details: str) -> JSONResponse:
        return JSONResponse(status_code=400, content={
            "messaging_product": "whatsapp", "success": False, "deleted": False,
            "details": details})

    if rules.PARENT_BSUID_RE.match(bsuid):
        return reject("Parent BSUIDs cannot be used with /contact_book; "
                      "use the portfolio-scoped BSUID.")
    row = (await ctx.session.execute(select(Bsuid).where(
        Bsuid.value == bsuid, Bsuid.api_key_id == ctx.key.id))).scalars().first()
    if row is None:
        return reject("Recipient does not exist or does not belong to this "
                      "business portfolio.")
    user = ctx.key.user
    deleted = user.contact_book
    user.contact_book = False
    user.contact_book_phone_known = False
    user.cache_at = None  # clears the 30-day cache for all numbers in the portfolio
    user.cache_phone_known = False
    await ctx.session.commit()
    return {"messaging_product": "whatsapp", "success": True, "deleted": deleted}


# ------------------------------------------------------ parent BSUID accounts

@router.get(
    "/parent-bsuid-accounts", tags=["Contact book"],
    summary="Read the parent BSUID account",
    description="Returns the parent BSUID account and enrolled portfolios when "
                "`user.parent_bsuid=true` in the sandbox config; empty otherwise.",
    responses={200: {"content": {"application/json": {"example": {
        "parent_bsuid_account_id": "400000000000042",
        "enrolled_business_portfolios": ["300000000000042"]}}}}})
async def get_parent_bsuid_accounts(ctx: Ctx = Depends(auth)):
    cfg = rules.effective_config(ctx.key)
    if not cfg["user"]["parent_bsuid"]:
        return {"parent_bsuid_account_id": None, "enrolled_business_portfolios": []}
    return {"parent_bsuid_account_id": "4" + ctx.key.portfolio_id[1:],
            "enrolled_business_portfolios": [ctx.key.portfolio_id]}


# ------------------------------------------------------------------ templates

def _validate_contact_info_buttons(components: list) -> None:
    for comp in components or []:
        if str(comp.get("type", "")).upper() != "BUTTONS":
            continue
        for btn in comp.get("buttons", []):
            if str(btn.get("type", "")).upper() != "REQUEST_CONTACT_INFO":
                continue
            text = btn.get("text")
            if not text:
                raise ApiError(100, "REQUEST_CONTACT_INFO button requires a 'text' field.",
                               subcode=2388050)
            if text != "Share Contact Info":
                raise ApiError(100, "REQUEST_CONTACT_INFO button text must be exactly "
                                    "'Share Contact Info'.",
                               subcode=2388153,
                               user_title="Button text modification not allowed")


@router.post(
    "/v1/configs/templates", tags=["Templates"],
    summary="Create a template (auto-approved)",
    description=(
        "Templates auto-approve instantly. Set `auth_flavor` "
        "(`one-tap`/`zero-tap`/`copy-code`) to mark a template as "
        "authentication-flavored — those cannot be sent to BSUIDs (`131062`).\n\n"
        "**Errors**: `100` for missing fields/bad category; REQUEST_CONTACT_INFO "
        "button validation: missing `text` → `error_subcode` **2388050**; text ≠ "
        "`\"Share Contact Info\"` → `error_subcode` **2388153**."
    ),
    openapi_extra={"requestBody": {"content": {"application/json": {"example": {
        "name": "recover_phone", "language": "en", "category": "utility",
        "components": [{"type": "BODY", "text": "Please share your contact info."},
                       {"type": "BUTTONS", "buttons": [
                           {"type": "REQUEST_CONTACT_INFO",
                            "text": "Share Contact Info"}]}]}}}}},
    responses={200: {"content": {"application/json": {"example": {
        "id": "tpl_000001", "name": "recover_phone", "language": "en",
        "category": "utility", "status": "approved"}}}}})
async def create_template(body: dict, ctx: Ctx = Depends(auth)):
    for field in ("name", "language", "category"):
        if not body.get(field):
            raise ApiError(100, f"Template field '{field}' is required.")
    if body["category"] not in ("utility", "marketing"):
        raise ApiError(100, "Template category must be 'utility' or 'marketing'.")
    flavor = body.get("auth_flavor")
    if flavor is not None and flavor not in ("one-tap", "zero-tap", "copy-code"):
        raise ApiError(100, "auth_flavor must be one of one-tap, zero-tap, copy-code.")
    _validate_contact_info_buttons(body.get("components", []))
    tpl = Template(id=await ids.new_id(ctx.session, "tpl"), api_key_id=ctx.key.id,
                   name=body["name"], language=body["language"],
                   category=body["category"], components=body.get("components", []),
                   status="approved", auth_flavor=flavor)
    ctx.session.add(tpl)
    await ctx.session.commit()
    return {"id": tpl.id, "name": tpl.name, "language": tpl.language,
            "category": tpl.category, "status": tpl.status}


@router.get(
    "/v1/configs/templates", tags=["Templates"],
    summary="List this number's templates",
    responses={200: {"content": {"application/json": {"example": {"data": [{
        "id": "tpl_000001", "name": "recover_phone", "language": "en",
        "category": "utility", "status": "approved", "auth_flavor": None,
        "components": []}]}}}}})
async def list_templates(ctx: Ctx = Depends(auth)):
    rows = (await ctx.session.execute(select(Template).where(
        Template.api_key_id == ctx.key.id))).scalars().all()
    return {"data": [{"id": t.id, "name": t.name, "language": t.language,
                      "category": t.category, "status": t.status,
                      "auth_flavor": t.auth_flavor, "components": t.components}
                     for t in rows]}
