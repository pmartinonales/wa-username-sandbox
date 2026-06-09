"""Webhook payload builders (§9) and the async delivery engine (§9.1).

Every webhook is recorded in WebhookDelivery (the Simulation API exposes the
log), and additionally POSTed to the business number's webhook_url when one is
set, with up to WEBHOOK_MAX_ATTEMPTS retries and exponential backoff.

Delayed status webhooks are scheduled as asyncio tasks; visibility is
re-evaluated at delivery time so config flips and contact shares affect the
very next status. Pending delayed webhooks do not survive a restart
(documented simplification).
"""
import asyncio
import hashlib
import hmac
import json

import httpx

from app import ids, settings
from app.db import SessionLocal
from app.errors import TITLES
from app.models import (
    BusinessNumber, Consumer, Message, Portfolio, Tenant, WebhookDelivery,
)
from app import rules

PENDING: set[asyncio.Task] = set()


async def wait_for_pending() -> None:
    """Test helper: wait until all scheduled webhook tasks have finished."""
    while PENDING:
        await asyncio.gather(*list(PENDING), return_exceptions=True)


def envelope(waba_id: str, field: str, value: dict) -> dict:
    return {"object": "whatsapp_business_account",
            "entry": [{"id": waba_id, "changes": [{"value": value, "field": field}]}]}


def metadata(bn: BusinessNumber) -> dict:
    return {"display_phone_number": bn.display_phone_number, "phone_number_id": bn.id}


def build_contact(consumer: Consumer, bsuid: str, *, visible: bool,
                  parent_user_id: str | None, include_username: bool = True) -> dict:
    profile = {"name": consumer.display_name}
    if consumer.username and include_username:
        profile["username"] = consumer.username  # no '@' prefix
    contact = {"profile": profile}
    if visible:
        contact["wa_id"] = consumer.phone
    contact["user_id"] = bsuid
    if parent_user_id:
        contact["parent_user_id"] = parent_user_id
    return contact


def conversation_block(wamid: str, category: str) -> dict:
    return {"id": hashlib.sha256(wamid.encode()).hexdigest()[:24],
            "origin": {"type": category}}


# ----------------------------------------------------------------- dispatch

async def record_and_send(session, bn: BusinessNumber, tenant_id: str, field: str,
                          payload: dict) -> None:
    """Create the delivery-log row inside the caller's session, then deliver."""
    row = WebhookDelivery(id=await ids.new_id(session, "wd"), tenant_id=tenant_id,
                          business_number_id=bn.id, endpoint=bn.webhook_url, field=field,
                          payload=payload, created_at=rules.now_for(await session.get(Tenant, tenant_id)),
                          status="pending")
    session.add(row)
    await session.flush()
    _spawn(_deliver(row.id, bn.webhook_url, bn.webhook_secret, payload))


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    PENDING.add(task)
    task.add_done_callback(PENDING.discard)


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


# ----------------------------------------------------- status webhooks (§9.2)

def schedule_status_webhooks(message_id: str, cfg: dict) -> None:
    sequence = cfg["status_sequence"]
    delays = cfg["status_delays_ms"]
    # one task per message: statuses are emitted strictly in order (delays are
    # measured from send time, so we sleep the increments between them)
    _spawn(_emit_sequence(message_id, list(sequence), list(delays),
                          int(cfg["failed_error_code"])))


async def _emit_sequence(message_id: str, sequence: list[str], delays: list[int],
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


async def emit_status(session, message_id: str, status: str, failed_code: int = 131049,
                      errors: list | None = None) -> None:
    """Build + dispatch one status webhook; state (visibility, contact book) is
    evaluated now, not at send time."""
    msg = await session.get(Message, message_id)
    bn = await session.get(BusinessNumber, msg.business_number_id)
    portfolio = await session.get(Portfolio, bn.portfolio_id)
    tenant = await session.get(Tenant, portfolio.tenant_id)
    consumer = await session.get(Consumer, msg.consumer_id)
    cfg = rules.effective_config(bn, tenant)

    # §6: a BSUID send that gets delivered writes contact book + cache —
    # flagged phone_known=False so it never reveals the phone (§12.1).
    if status == "delivered" and msg.addressed_by == "bsuid":
        await rules.touch_contact(session, portfolio, bn, consumer,
                                  rules.now_for(tenant), phone_known=False)
    if status == "delivered" and msg.addressed_by == "phone":
        await rules.touch_contact(session, portfolio, bn, consumer, rules.now_for(tenant))

    bsuid = await rules.get_or_create_bsuid(session, consumer, portfolio)
    parent = await rules.parent_id_for(session, consumer, portfolio, cfg, tenant)
    ts = str(int(rules.now_for(tenant).timestamp()))
    category = msg.payload.get("_category", "service")

    status_obj = {"id": msg.wamid, "status": status, "timestamp": ts}
    if status == "failed":
        # §9.2/§9.4: no contacts array; phone-addressed → no recipient_user_id
        if msg.addressed_by == "phone":
            status_obj["recipient_id"] = consumer.phone
        else:
            status_obj["recipient_user_id"] = bsuid
            if parent:
                status_obj["recipient_parent_user_id"] = parent
        code = errors[0]["code"] if errors else failed_code
        status_obj["errors"] = errors or [{
            "code": code, "title": TITLES.get(code, "Message failed"),
            "error_data": {"details": "Sandbox-simulated delivery failure."}}]
        value = {"messaging_product": "whatsapp", "metadata": metadata(bn),
                 "statuses": [status_obj]}
    else:
        visible = (msg.addressed_by == "phone") or await rules.phone_visible(
            session, bn, consumer, portfolio, tenant, cfg)
        if visible:
            status_obj["recipient_id"] = consumer.phone
        status_obj["recipient_user_id"] = bsuid
        if parent:
            status_obj["recipient_parent_user_id"] = parent
        status_obj["conversation"] = conversation_block(msg.wamid, category)
        status_obj["pricing"] = {"billable": True, "pricing_model": "PMP",
                                 "category": category, "type": "regular"}
        # §9.4: username on delivered/read only, never on sent
        include_username = status in ("delivered", "read")
        contact = build_contact(consumer, bsuid, visible=visible, parent_user_id=parent,
                                include_username=include_username)
        value = {"messaging_product": "whatsapp", "metadata": metadata(bn),
                 "contacts": [contact], "statuses": [status_obj]}

    msg.statuses = [*(msg.statuses or []), {"status": status, "timestamp": ts}]
    await record_and_send(session, bn, tenant.id, "messages", envelope(portfolio.id, "messages", value))


# ---------------------------------------------------- inbound webhooks (§9.3)

async def emit_inbound_message(session, bn: BusinessNumber, consumer: Consumer,
                               wamid: str, messages_value: dict) -> None:
    portfolio = await session.get(Portfolio, bn.portfolio_id)
    tenant = await session.get(Tenant, portfolio.tenant_id)
    cfg = rules.effective_config(bn, tenant)
    visible = await rules.phone_visible(session, bn, consumer, portfolio, tenant, cfg)
    bsuid = await rules.get_or_create_bsuid(session, consumer, portfolio)
    parent = await rules.parent_id_for(session, consumer, portfolio, cfg, tenant)
    msg = {**messages_value, "id": wamid,
           "timestamp": str(int(rules.now_for(tenant).timestamp()))}
    if visible:
        msg["from"] = consumer.phone
    msg["from_user_id"] = bsuid
    value = {"messaging_product": "whatsapp", "metadata": metadata(bn),
             "contacts": [build_contact(consumer, bsuid, visible=visible, parent_user_id=parent)],
             "messages": [msg]}
    await record_and_send(session, bn, tenant.id, "messages", envelope(portfolio.id, "messages", value))


async def emit_system_phone_change(session, bn: BusinessNumber, consumer: Consumer,
                                   old_bsuid: str, new_bsuid: str) -> None:
    portfolio = await session.get(Portfolio, bn.portfolio_id)
    tenant = await session.get(Tenant, portfolio.tenant_id)
    wamid = await ids.new_wamid(session)
    value = {"messaging_product": "whatsapp", "metadata": metadata(bn),
             "messages": [{"id": wamid, "timestamp": str(int(rules.now_for(tenant).timestamp())),
                           "type": "system",
                           "system": {"body": "User changed phone number",
                                      "user_id": old_bsuid, "new_user_id": new_bsuid}}]}
    await record_and_send(session, bn, tenant.id, "messages", envelope(portfolio.id, "messages", value))


async def emit_business_username_update(session, bn: BusinessNumber, status: str) -> None:
    portfolio = await session.get(Portfolio, bn.portfolio_id)
    tenant = await session.get(Tenant, portfolio.tenant_id)
    value = {"display_phone_number": bn.display_phone_number, "status": status}
    if status != "deleted":
        value["username"] = bn.username
    await record_and_send(session, bn, tenant.id, "business_username_update",
                          envelope(portfolio.id, "business_username_update", value))
