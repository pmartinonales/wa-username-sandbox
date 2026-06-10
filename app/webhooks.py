"""Webhook payload builders (v1 §9) and the async delivery engine, plus the
simulated-consumer actions (§3.3 consumer_actions) that replace v1's
Simulation API: auto-replies, auto-taps on REQUEST_CONTACT_INFO, manual shares.

Every webhook is recorded in WebhookDelivery (GET /sandbox/webhook exposes the
last 50) and POSTed to the configured URL with retries + HMAC signature.
Identifier inclusion follows the v1 §9.4 matrix exactly; visibility is
re-evaluated at delivery time, so config flips affect the very next webhook.
Pending delayed webhooks do not survive a restart (documented simplification).
"""
import asyncio
import hashlib
import hmac
import json

import httpx

from app import ids, rules, settings
from app.db import SessionLocal
from app.errors import TITLES
from app.models import ApiKey, Message, WebhookDelivery

PENDING: set[asyncio.Task] = set()


async def wait_for_pending() -> None:
    """Test helper: wait until all scheduled webhook tasks have finished."""
    while PENDING:
        await asyncio.gather(*list(PENDING), return_exceptions=True)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    PENDING.add(task)
    task.add_done_callback(PENDING.discard)


def envelope(key: ApiKey, field: str, value: dict) -> dict:
    return {"object": "whatsapp_business_account",
            "entry": [{"id": key.waba_id, "changes": [{"value": value, "field": field}]}]}


def metadata(key: ApiKey) -> dict:
    return {"display_phone_number": key.display_phone_number,
            "phone_number_id": key.phone_number_id}


def build_contact(key: ApiKey, cfg: dict, *, visible: bool,
                  include_username: bool = True) -> dict:
    user = key.user
    profile = {"name": user.display_name}
    username = rules.user_username(key, cfg)
    if username and include_username:
        profile["username"] = username  # no '@' prefix
    contact = {"profile": profile}
    if visible:
        contact["wa_id"] = user.phone
    contact["user_id"] = user.bsuid
    parent = rules.parent_id(key, cfg)
    if parent:
        contact["parent_user_id"] = parent
    return contact


def conversation_block(wamid: str, category: str) -> dict:
    return {"id": hashlib.sha256(wamid.encode()).hexdigest()[:24],
            "origin": {"type": category}}


# ----------------------------------------------------------------- dispatch

async def record_and_send(session, key: ApiKey, field: str, value: dict) -> None:
    """Create the delivery-log row in the caller's session, then deliver async."""
    payload = envelope(key, field, value)
    row = WebhookDelivery(id=await ids.new_id(session, "wd"), api_key_id=key.id,
                          endpoint=key.webhook_url, field=field, payload=payload,
                          created_at=rules.now(), status="pending")
    session.add(row)
    await session.flush()
    _spawn(_deliver(row.id, key.webhook_url, key.webhook_secret, payload))


async def _get_row(session, row_id: str) -> WebhookDelivery | None:
    # the row is flushed in the caller's session but may not be committed yet
    # when this task first runs — poll briefly before giving up
    for _ in range(40):
        row = await session.get(WebhookDelivery, row_id)
        if row is not None:
            return row
        await asyncio.sleep(0.05)
    return None


async def _deliver(row_id: str, url: str | None, secret: str | None, payload: dict) -> None:
    if url is None:
        async with SessionLocal() as session:
            row = await _get_row(session, row_id)
            if row is None:
                return  # caller rolled back
            row.status = "logged"  # no endpoint configured; payload kept for the log
            await session.commit()
        return
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Sandbox-Signature"] = f"sha256={sig}"
    attempts, code, ok = 0, None, False
    async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_S) as client:
        for i in range(settings.WEBHOOK_MAX_ATTEMPTS):
            attempts = i + 1
            try:
                resp = await client.post(url, content=body, headers=headers)
                code = resp.status_code
                if 200 <= code < 300:
                    ok = True
                    break
            except httpx.HTTPError:
                code = None
            if i < settings.WEBHOOK_MAX_ATTEMPTS - 1:
                await asyncio.sleep(settings.WEBHOOK_BACKOFF_BASE_S * (2 ** i))
    async with SessionLocal() as session:
        row = await _get_row(session, row_id)
        if row is None:
            return
        row.attempts, row.response_code = attempts, code
        row.status = "delivered" if ok else "failed"
        await session.commit()


# ------------------------------------------------ status webhooks (v1 §9.2)

def schedule_message_lifecycle(message_id: str, cfg: dict) -> None:
    """One task per message: emit the configured status sequence in order, then
    trigger any configured consumer actions after 'delivered'."""
    _spawn(_lifecycle(message_id, list(cfg["statuses"]["sequence"]),
                      list(cfg["statuses"]["delays_ms"]),
                      int(cfg["statuses"]["failed_error_code"])))


async def _lifecycle(message_id: str, sequence: list[str], delays: list[int],
                     failed_code: int) -> None:
    elapsed_ms = 0
    for i, status in enumerate(sequence):
        delay_ms = delays[i] if i < len(delays) else (delays[-1] if delays else 0)
        if delay_ms > elapsed_ms:
            await asyncio.sleep((delay_ms - elapsed_ms) / 1000.0)
            elapsed_ms = delay_ms
        async with SessionLocal() as session:
            await emit_status(session, message_id, status, failed_code)
            await session.commit()
        if status == "delivered":
            _spawn(_consumer_actions(message_id))


async def emit_status(session, message_id: str, status: str,
                      failed_code: int = 131049, errors: list | None = None) -> None:
    """Build + dispatch one status webhook; visibility and contact-book state
    are evaluated now, not at send time."""
    msg = await session.get(Message, message_id)
    key = await session.get(ApiKey, msg.api_key_id)
    cfg = rules.effective_config(key)
    user = key.user

    # v1 §6: a delivered BSUID send writes contact book + cache, but flagged
    # phone_known=False so it never reveals the phone; delivered phone sends
    # refresh with phone_known=True.
    if status == "delivered":
        rules.touch_contact(user, rules.now(), phone_known=(msg.addressed_by == "phone"))

    parent = rules.parent_id(key, cfg)
    ts = str(int(rules.now().timestamp()))
    category = msg.payload.get("_category", "service")

    status_obj = {"id": msg.wamid, "status": status, "timestamp": ts}
    if status == "failed":
        # v1 §9.2: no contacts array; phone-addressed → no recipient_user_id
        if msg.addressed_by == "phone":
            status_obj["recipient_id"] = user.phone
        else:
            status_obj["recipient_user_id"] = user.bsuid
            if parent:
                status_obj["recipient_parent_user_id"] = parent
        code = errors[0]["code"] if errors else failed_code
        status_obj["errors"] = errors or [{
            "code": code, "title": TITLES.get(code, "Message failed"),
            "error_data": {"details": "Sandbox-simulated delivery failure."}}]
        value = {"messaging_product": "whatsapp", "metadata": metadata(key),
                 "statuses": [status_obj]}
    else:
        # Meta's identifier quick reference: phone-addressed messages ALWAYS
        # carry the phone in statuses (the business already knows it);
        # phone_visibility — forced or auto — only gates BSUID-addressed ones
        visible = (msg.addressed_by == "phone") or rules.phone_visible(key, cfg)
        if visible:
            status_obj["recipient_id"] = user.phone
        status_obj["recipient_user_id"] = user.bsuid
        if parent:
            status_obj["recipient_parent_user_id"] = parent
        status_obj["conversation"] = conversation_block(msg.wamid, category)
        status_obj["pricing"] = {"billable": True, "pricing_model": "PMP",
                                 "category": category, "type": "regular"}
        # v1 §9.4: username on delivered/read only, never on sent
        contact = build_contact(key, cfg, visible=visible,
                                include_username=status in ("delivered", "read"))
        value = {"messaging_product": "whatsapp", "metadata": metadata(key),
                 "contacts": [contact], "statuses": [status_obj]}

    msg.statuses = [*(msg.statuses or []), {"status": status, "timestamp": ts}]
    await record_and_send(session, key, "messages", value)


# ------------------------------------- simulated consumer actions (§3.3)

def is_request_contact_info(session_templates: list, msg: Message) -> bool:
    if msg.type == "interactive":
        return (msg.payload.get("interactive") or {}).get("type") == "request_contact_info"
    if msg.type == "template":
        for t in session_templates:
            if t.name == (msg.payload.get("template") or {}).get("name"):
                return any(str(b.get("type", "")).upper() == "REQUEST_CONTACT_INFO"
                           for comp in (t.components or [])
                           if str(comp.get("type", "")).upper() == "BUTTONS"
                           for b in comp.get("buttons", []))
    return False


async def _consumer_actions(message_id: str) -> None:
    """After a delivered outbound message, the simulated user acts per config."""
    from sqlalchemy import select

    from app.models import Template

    async with SessionLocal() as session:
        msg = await session.get(Message, message_id)
        key = await session.get(ApiKey, msg.api_key_id)
        cfg = rules.effective_config(key)
        actions = cfg["consumer_actions"]
        templates = (await session.execute(select(Template).where(
            Template.api_key_id == key.id))).scalars().all()
        rci = is_request_contact_info(templates, msg)

    # each action delays independently from the 'delivered' moment
    if rci and actions["tap_request_contact_info"]:
        _spawn(_delayed_share(msg.api_key_id, "contact_request",
                              actions["tap_delay_ms"]))
    if actions["reply_to_messages"]:
        _spawn(_delayed_reply(msg.api_key_id, actions["reply_text"],
                              actions["reply_delay_ms"]))
    if actions["share_contact_manually"]:
        _spawn(_delayed_share(msg.api_key_id, "other", actions["tap_delay_ms"]))


async def _delayed_share(key_id: str, origin: str, delay_ms: int) -> None:
    await asyncio.sleep(delay_ms / 1000.0)
    async with SessionLocal() as session:
        await emit_contacts_share(session, key_id, origin=origin)
        await session.commit()


async def _delayed_reply(key_id: str, text: str, delay_ms: int) -> None:
    await asyncio.sleep(delay_ms / 1000.0)
    async with SessionLocal() as session:
        await emit_inbound_reply(session, key_id, text)
        await session.commit()


async def emit_inbound_reply(session, key_id: str, text: str) -> str:
    """The simulated user sends a message: inbound webhook (v1 §9.3), opens the
    24h window, writes contact book + cache when the phone is visible (v1 §6)."""
    key = await session.get(ApiKey, key_id)
    cfg = rules.effective_config(key)
    user = key.user
    when = rules.now()
    wamid = await ids.new_wamid(session)
    session.add(Message(id=await ids.new_id(session, "msg"), wamid=wamid,
                        direction="inbound", api_key_id=key.id, addressed_by="phone",
                        type="text", payload={"text": {"body": text}},
                        statuses=[], created_at=when))
    user.window_opened_at = when
    visible = rules.phone_visible(key, cfg)
    if visible:
        rules.touch_contact(user, when, phone_known=True)
    msg = {"type": "text", "text": {"body": text}, "id": wamid,
           "timestamp": str(int(when.timestamp()))}
    if visible:
        msg["from"] = user.phone
    msg["from_user_id"] = user.bsuid
    value = {"messaging_product": "whatsapp", "metadata": metadata(key),
             "contacts": [build_contact(key, cfg, visible=visible)],
             "messages": [msg]}
    await record_and_send(session, key, "messages", value)
    return wamid


async def emit_contacts_share(session, key_id: str, origin: str) -> str:
    """v1 §9.5 contacts webhook; vCard only when origin='other'; a
    contact_request share writes contact book + cache (phone visible after)."""
    key = await session.get(ApiKey, key_id)
    cfg = rules.effective_config(key)
    user = key.user
    when = rules.now()
    wamid = await ids.new_wamid(session)
    shared = {"name": {"formatted_name": user.display_name,
                       "first_name": user.display_name.split(" ")[0]},
              "phones": [{"phone": f"+{user.phone}", "wa_id": user.phone,
                          "type": "MOBILE"}],
              "origin": origin}
    if origin == "other":
        shared["vcard"] = (f"BEGIN:VCARD\nVERSION:3.0\nFN:{user.display_name}\n"
                           f"TEL;TYPE=CELL:+{user.phone}\nEND:VCARD")
    visible = rules.phone_visible(key, cfg)
    msg = {"id": wamid, "timestamp": str(int(when.timestamp())), "type": "contacts",
           "from_user_id": user.bsuid, "contacts": [shared]}
    if visible:
        msg["from"] = user.phone
    value = {"messaging_product": "whatsapp", "metadata": metadata(key),
             "contacts": [build_contact(key, cfg, visible=visible)],
             "messages": [msg]}
    await record_and_send(session, key, "messages", value)
    if origin == "contact_request":
        rules.touch_contact(user, when, phone_known=True)
    return wamid


async def emit_system_phone_change(session, key: ApiKey, old_bsuid: str,
                                   new_bsuid: str) -> None:
    wamid = await ids.new_wamid(session)
    value = {"messaging_product": "whatsapp", "metadata": metadata(key),
             "messages": [{"id": wamid, "timestamp": str(int(rules.now().timestamp())),
                           "type": "system",
                           "system": {"body": "User changed phone number",
                                      "user_id": old_bsuid, "new_user_id": new_bsuid}}]}
    await record_and_send(session, key, "messages", value)


async def emit_business_username_update(session, key: ApiKey, status: str) -> None:
    value = {"display_phone_number": key.display_phone_number, "status": status}
    if status != "deleted":
        value["username"] = key.username
    await record_and_send(session, key, "business_username_update", value)
