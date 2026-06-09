"""Partner-facing Mock API (§8) — auth via D360-API-KEY, key scopes the number."""
from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app import ids, rules, webhooks
from app.deps import MockContext, mock_auth
from app.errors import ApiError
from app.models import (
    BsuidMapping, BusinessNumber, CacheEntry, ContactBookEntry, Message,
    ParentEnrollment, Portfolio, Template, UsernameChange,
)

router = APIRouter(tags=["Mock API"])


# ------------------------------------------------------------- POST /messages

async def _find_template(ctx: MockContext, body: dict) -> Template:
    tpl_req = body.get("template") or {}
    name = tpl_req.get("name")
    if not name:
        raise ApiError(100, "template.name is required for template sends.")
    q = select(Template).where(Template.portfolio_id == ctx.portfolio.id, Template.name == name)
    lang = (tpl_req.get("language") or {}).get("code")
    if lang:
        q = q.where(Template.language == lang)
    tpl = (await ctx.session.execute(q)).scalars().first()
    if tpl is None:
        raise ApiError(132001, f"Template '{name}' does not exist for this portfolio.", 404)
    return tpl


async def _send(ctx: MockContext, body: dict, *, marketing: bool) -> dict:
    session, cfg = ctx.session, rules.effective_config(ctx.bn, ctx.tenant)
    await rules.pop_injected_error(session, ctx.bn, "marketing_messages" if marketing else "messages")

    to, recipient = body.get("to"), body.get("recipient")
    if not to and not recipient:
        raise ApiError(100, "Provide 'to' (phone) or 'recipient' (BSUID).")
    addressed_by = "phone" if to else "bsuid"  # §8.1.1: 'to' wins when both present

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
            raise ApiError(100, f"Template '{template.name}' is category '{template.category}'; "
                                "/marketing_messages requires a marketing template.")

    if mtype == "interactive":
        itype = (body.get("interactive") or {}).get("type")
        if itype == "contact_request":
            raise ApiError(100, "'contact_request' is not a valid interactive type. "
                                "Use 'request_contact_info'.")
        if itype != "request_contact_info":
            raise ApiError(100, f"interactive.type '{itype}' is not implemented in the sandbox. "
                                "Use 'request_contact_info'.")

    if addressed_by == "phone":
        consumer = await rules.resolve_phone_recipient(session, ctx.tenant, cfg, str(to))
        bsuid = None
    else:
        consumer, bsuid = await rules.resolve_bsuid_recipient(
            session, ctx.tenant, ctx.portfolio, cfg, str(recipient))

    # §8.1.4: auth-flavored templates cannot target BSUIDs
    if template is not None and template.auth_flavor and addressed_by == "bsuid":
        raise ApiError(131062, "Business-scoped User ID (BSUID) recipients are not supported "
                               "for this message.")

    # §8.1.5: free-form sends require an open 24h service window
    if mtype != "template" and not await rules.window_open(session, ctx.bn, consumer, ctx.tenant, cfg):
        raise ApiError(131047, "Message outside the 24-hour service window. "
                               "Use an approved template.")

    now = rules.now_for(ctx.tenant)
    wamid = await ids.new_wamid(session)
    payload = {k: v for k, v in body.items() if k != "messaging_product"}
    payload["_category"] = category
    msg = Message(id=await ids.new_id(session, "msg"), wamid=wamid, direction="outbound",
                  business_number_id=ctx.bn.id, consumer_id=consumer.id,
                  addressed_by=addressed_by, type=mtype, payload=payload,
                  statuses=[], created_at=now)
    session.add(msg)
    await session.flush()

    if addressed_by == "phone":
        # §6: phone traffic writes contact book + cache immediately;
        # BSUID sends write only on delivery (handled in emit_status).
        await rules.touch_contact(session, ctx.portfolio, ctx.bn, consumer, now)
    await session.commit()
    webhooks.schedule_status_webhooks(msg.id, cfg)

    message_obj: dict = {"id": wamid}
    if marketing:
        message_obj["message_status"] = "accepted"
    if addressed_by == "phone":
        contact = {"input": str(to), "wa_id": consumer.phone}
    else:
        contact = {"input": str(recipient), "user_id": bsuid}
    return {"messaging_product": "whatsapp", "contacts": [contact], "messages": [message_obj]}


@router.post("/messages")
async def post_messages(body: dict, ctx: MockContext = Depends(mock_auth)):
    return await _send(ctx, body, marketing=False)


@router.post("/marketing_messages")
async def post_marketing_messages(body: dict, ctx: MockContext = Depends(mock_auth)):
    return await _send(ctx, body, marketing=True)


# --------------------------------------------------- username management (§8.3)

@router.get("/username_suggestions")
async def username_suggestions(ctx: MockContext = Depends(mock_auth)):
    base = "".join(c for c in ctx.bn.name.lower() if c.isalnum()) or "business"
    base = base[:20]
    suggestions = [base, f"{base}.official", f"{base}{ids.digits(3, 'sugg', ctx.bn.id)}"]
    return {"data": [{"username_suggestions": suggestions}]}


@router.post("/username")
async def claim_username(body: dict, ctx: MockContext = Depends(mock_auth)):
    session = ctx.session
    await rules.pop_injected_error(session, ctx.bn, "username")
    name = body.get("username")
    rules.validate_username_format(name)

    # rate limit BEFORE uniqueness: a rate-limited claim must yield the
    # dedicated 429, never "already claimed" (the production bug this corrects)
    now = rules.now_for(ctx.tenant)
    cutoff = now - timedelta(days=14)
    changes = (await session.execute(select(UsernameChange).where(
        UsernameChange.business_number_id == ctx.bn.id,
        UsernameChange.changed_at > cutoff).order_by(UsernameChange.changed_at))).scalars().all()
    if len(changes) >= 3:
        next_at = (changes[0].changed_at + timedelta(days=14)).isoformat() + "Z"
        raise ApiError(131056, "Username change limit reached: max 3 changes per 14 days. "
                               f"Next change available at {next_at}.", http_status=429)

    await rules.assert_username_available(session, name, exclude_bn=ctx.bn.id)

    ctx.bn.username = name
    ctx.bn.username_status = "approved" if ctx.tenant.ga_mode else "reserved"
    session.add(UsernameChange(id=await ids.new_id(session, "uc"),
                               business_number_id=ctx.bn.id, changed_at=now))
    status = ctx.bn.username_status
    await webhooks.emit_business_username_update(session, ctx.bn, status)
    await session.commit()
    return {"status": status}


@router.get("/username")
async def get_username(ctx: MockContext = Depends(mock_auth)):
    if not ctx.bn.username:
        return {}
    status = "active" if ctx.tenant.ga_mode else ctx.bn.username_status
    return {"username": ctx.bn.username, "status": status}


@router.delete("/username")
async def delete_username(ctx: MockContext = Depends(mock_auth)):
    had = ctx.bn.username is not None
    if had:
        await webhooks.emit_business_username_update(ctx.session, ctx.bn, "deleted")
        ctx.bn.username = None
        ctx.bn.username_status = None
        await ctx.session.commit()
    return {"success": had}


# ------------------------------------------------------ DELETE /contact_book (§6)

@router.delete("/contact_book")
async def delete_contact_book(bsuid: str, messaging_product: str = "whatsapp",
                              ctx: MockContext = Depends(mock_auth)):
    session = ctx.session

    def reject(details: str) -> JSONResponse:  # §6: success:false, 400 (not the error envelope)
        return JSONResponse(status_code=400, content={
            "messaging_product": "whatsapp", "success": False, "deleted": False,
            "details": details})

    if rules.PARENT_BSUID_RE.match(bsuid):
        return reject("Parent BSUIDs cannot be used with /contact_book; "
                      "use the portfolio-scoped BSUID.")
    mapping = (await session.execute(select(BsuidMapping).where(
        BsuidMapping.bsuid == bsuid,
        BsuidMapping.portfolio_id == ctx.portfolio.id))).scalar_one_or_none()
    if mapping is None:
        return reject("Recipient does not exist or does not belong to this "
                      "business portfolio.")
    entry = (await session.execute(select(ContactBookEntry).where(
        ContactBookEntry.portfolio_id == ctx.portfolio.id,
        ContactBookEntry.consumer_id == mapping.consumer_id))).scalar_one_or_none()
    deleted = entry is not None
    if entry:
        await session.delete(entry)
    # clear the 30-day cache for ALL numbers in the portfolio (alpha-confirmed)
    number_ids = (await session.execute(select(BusinessNumber.id).where(
        BusinessNumber.portfolio_id == ctx.portfolio.id))).scalars().all()
    caches = (await session.execute(select(CacheEntry).where(
        CacheEntry.business_number_id.in_(number_ids),
        CacheEntry.consumer_id == mapping.consumer_id))).scalars().all()
    for c in caches:
        await session.delete(c)
    await session.commit()
    return {"messaging_product": "whatsapp", "success": True, "deleted": deleted}


# -------------------------------------------------------- templates (§8.5)

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
                               subcode=2388153, user_title="Button text modification not allowed")


@router.post("/v1/configs/templates")
async def create_template(body: dict, ctx: MockContext = Depends(mock_auth)):
    for field in ("name", "language", "category"):
        if not body.get(field):
            raise ApiError(100, f"Template field '{field}' is required.")
    if body["category"] not in ("utility", "marketing"):
        raise ApiError(100, "Template category must be 'utility' or 'marketing'.")
    flavor = body.get("auth_flavor")
    if flavor is not None and flavor not in ("one-tap", "zero-tap", "copy-code"):
        raise ApiError(100, "auth_flavor must be one of one-tap, zero-tap, copy-code.")
    _validate_contact_info_buttons(body.get("components", []))
    tpl = Template(id=await ids.new_id(ctx.session, "tpl"), portfolio_id=ctx.portfolio.id,
                   name=body["name"], language=body["language"], category=body["category"],
                   components=body.get("components", []), status="approved", auth_flavor=flavor)
    ctx.session.add(tpl)
    await ctx.session.commit()
    return {"id": tpl.id, "name": tpl.name, "language": tpl.language,
            "category": tpl.category, "status": tpl.status}


@router.get("/v1/configs/templates")
async def list_templates(ctx: MockContext = Depends(mock_auth)):
    rows = (await ctx.session.execute(select(Template).where(
        Template.portfolio_id == ctx.portfolio.id))).scalars().all()
    return {"data": [{"id": t.id, "name": t.name, "language": t.language,
                      "category": t.category, "status": t.status,
                      "auth_flavor": t.auth_flavor, "components": t.components}
                     for t in rows]}


# --------------------------------------------------------------- configs (§8.7)

@router.post("/configs/webhook")
async def set_webhook(body: dict, ctx: MockContext = Depends(mock_auth)):
    url = body.get("url")
    if not url:
        raise ApiError(100, "Field 'url' is required.")
    ctx.bn.webhook_url = url
    ctx.bn.webhook_secret = body.get("secret")
    await ctx.session.commit()
    return {"url": ctx.bn.webhook_url, "secret_set": ctx.bn.webhook_secret is not None}


@router.get("/configs/webhook")
async def get_webhook(ctx: MockContext = Depends(mock_auth)):
    return {"url": ctx.bn.webhook_url, "secret_set": ctx.bn.webhook_secret is not None}


@router.put("/configs/behavior")
async def set_behavior_self_service(body: dict, ctx: MockContext = Depends(mock_auth)):
    rules.validate_behavior_patch(body)
    ctx.bn.behavior.config = {**(ctx.bn.behavior.config or {}), **body}
    flag_modified(ctx.bn.behavior, "config")
    await ctx.session.commit()
    return rules.effective_config(ctx.bn, ctx.tenant)


@router.get("/configs/behavior")
async def get_behavior_self_service(ctx: MockContext = Depends(mock_auth)):
    return rules.effective_config(ctx.bn, ctx.tenant)


# ------------------------------------------------- parent BSUID accounts (§7.2)

@router.get("/parent-bsuid-accounts")
async def get_parent_bsuid_accounts(ctx: MockContext = Depends(mock_auth)):
    account = await rules.portfolio_parent_account(ctx.session, ctx.portfolio)
    if account is None:
        return {"parent_bsuid_account_id": None, "enrolled_business_portfolios": []}
    portfolio_ids = (await ctx.session.execute(select(ParentEnrollment.portfolio_id).where(
        ParentEnrollment.parent_account_id == account.id))).scalars().all()
    return {"parent_bsuid_account_id": account.id,
            "enrolled_business_portfolios": list(portfolio_ids)}
