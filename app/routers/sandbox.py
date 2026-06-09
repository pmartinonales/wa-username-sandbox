"""Simulation API (§10) — tester control plane, auth via SANDBOX-API-KEY."""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from app import ids, rules, webhooks
from app.db import get_session
from app.deps import SandboxContext, master_auth, sandbox_auth
from app.errors import ApiError
from app.models import (
    BehaviorConfig, BsuidMapping, BusinessNumber, CacheEntry, Consumer,
    ContactBookEntry, Message, ParentAccount, ParentBsuid, ParentEnrollment,
    Portfolio, ServiceWindow, Template, Tenant, WebhookDelivery,
)

router = APIRouter(prefix="/sandbox", tags=["Simulation API"])


# ----------------------------------------------------------------- admin

@router.post("/tenants", dependencies=[Depends(master_auth)])
async def create_tenant(body: dict, session=Depends(get_session)):
    tid = await ids.new_id(session, "tn")
    key = ids.make_api_key("sandbox", await ids.next_seq(session, "sandbox_key"))
    tenant = Tenant(id=tid, name=body.get("name", tid), sandbox_api_key=key,
                    ga_mode=bool(body.get("ga_mode", False)))
    session.add(tenant)
    await session.commit()
    return {"id": tid, "name": tenant.name, "sandbox_api_key": key, "ga_mode": tenant.ga_mode}


# ------------------------------------------------------- portfolios & numbers

@router.post("/portfolios")
async def create_portfolio(body: dict, ctx: SandboxContext = Depends(sandbox_auth)):
    pid = await ids.new_id(ctx.session, "pf")
    portfolio = Portfolio(id=pid, tenant_id=ctx.tenant.id, name=body.get("name", pid))
    ctx.session.add(portfolio)
    await ctx.session.commit()
    return {"id": pid, "name": portfolio.name}


async def _own_portfolio(ctx: SandboxContext, portfolio_id: str) -> Portfolio:
    portfolio = await ctx.session.get(Portfolio, portfolio_id)
    if portfolio is None or portfolio.tenant_id != ctx.tenant.id:
        raise ApiError(100, f"Unknown portfolio '{portfolio_id}'.", http_status=404)
    return portfolio


async def _own_number(ctx: SandboxContext, number_id: str) -> BusinessNumber:
    bn = await ctx.session.get(BusinessNumber, number_id)
    if bn:
        portfolio = await ctx.session.get(Portfolio, bn.portfolio_id)
        if portfolio.tenant_id == ctx.tenant.id:
            return bn
    raise ApiError(100, f"Unknown business number '{number_id}'.", http_status=404)


async def _own_consumer(ctx: SandboxContext, consumer_id: str) -> Consumer:
    consumer = await ctx.session.get(Consumer, consumer_id)
    if consumer is None or consumer.tenant_id != ctx.tenant.id:
        raise ApiError(100, f"Unknown consumer '{consumer_id}'.", http_status=404)
    return consumer


@router.post("/numbers")
async def create_number(body: dict, ctx: SandboxContext = Depends(sandbox_auth)):
    session = ctx.session
    portfolio = await _own_portfolio(ctx, body.get("portfolio_id", ""))
    nid = await ids.new_id(session, "bn")
    key = ids.make_api_key("d360", await ids.next_seq(session, "d360_key"))
    bn = BusinessNumber(id=nid, portfolio_id=portfolio.id,
                        name=body.get("name", f"Business {nid[-6:]}"),
                        display_phone_number=body.get("display_phone_number")
                        or "49" + ids.digits(10, "biznum", nid),
                        d360_api_key=key, webhook_url=body.get("webhook_url"),
                        webhook_secret=body.get("webhook_secret"))
    session.add(bn)
    behavior = body.get("behavior") or {}
    rules.validate_behavior_patch(behavior)
    session.add(BehaviorConfig(business_number_id=nid, config=behavior))
    await session.commit()
    return {"id": nid, "portfolio_id": portfolio.id, "name": bn.name,
            "display_phone_number": bn.display_phone_number,
            "d360_api_key": key, "webhook_url": bn.webhook_url}


@router.put("/numbers/{number_id}/behavior")
async def set_behavior(number_id: str, body: dict, ctx: SandboxContext = Depends(sandbox_auth)):
    bn = await _own_number(ctx, number_id)
    rules.validate_behavior_patch(body)
    bn.behavior.config = {**(bn.behavior.config or {}), **body}
    flag_modified(bn.behavior, "config")
    await ctx.session.commit()
    return rules.effective_config(bn, ctx.tenant)


@router.get("/numbers/{number_id}/behavior")
async def get_behavior(number_id: str, ctx: SandboxContext = Depends(sandbox_auth)):
    bn = await _own_number(ctx, number_id)
    return rules.effective_config(bn, ctx.tenant)


# ----------------------------------------------------------------- consumers

@router.post("/consumers")
async def create_consumer(body: dict, ctx: SandboxContext = Depends(sandbox_auth)):
    session = ctx.session
    for field in ("phone", "display_name", "country"):
        if not body.get(field):
            raise ApiError(100, f"Field '{field}' is required.")
    username = body.get("username")
    if username:
        rules.validate_username_format(username)
        await rules.assert_username_available(session, username)
    cid = await ids.new_id(session, "cs")
    consumer = Consumer(id=cid, tenant_id=ctx.tenant.id,
                        phone="".join(c for c in body["phone"] if c.isdigit()),
                        display_name=body["display_name"], username=username,
                        country=body["country"].upper())
    session.add(consumer)
    await session.commit()
    return {"id": cid, "phone": consumer.phone, "display_name": consumer.display_name,
            "username": consumer.username, "country": consumer.country}


@router.post("/consumers/{consumer_id}/username")
async def consumer_set_username(consumer_id: str, body: dict,
                                ctx: SandboxContext = Depends(sandbox_auth)):
    consumer = await _own_consumer(ctx, consumer_id)
    name = body.get("username")
    rules.validate_username_format(name)
    await rules.assert_username_available(ctx.session, name, exclude_consumer=consumer.id)
    consumer.username = name  # BSUID unchanged (§7.1)
    await ctx.session.commit()
    return {"id": consumer.id, "username": consumer.username}


@router.delete("/consumers/{consumer_id}/username")
async def consumer_remove_username(consumer_id: str,
                                   ctx: SandboxContext = Depends(sandbox_auth)):
    consumer = await _own_consumer(ctx, consumer_id)
    consumer.username = None
    await ctx.session.commit()
    return {"id": consumer.id, "username": None}


@router.post("/consumers/{consumer_id}/change_phone")
async def change_phone(consumer_id: str, body: dict,
                       ctx: SandboxContext = Depends(sandbox_auth)):
    """New phone → regenerate all BSUIDs + system webhook to each affected number (§7.1)."""
    session = ctx.session
    consumer = await _own_consumer(ctx, consumer_id)
    new_phone = "".join(c for c in body.get("phone", "") if c.isdigit())
    if not new_phone:
        raise ApiError(100, "Field 'phone' is required.")
    consumer.phone = new_phone
    mappings = (await session.execute(select(BsuidMapping).where(
        BsuidMapping.consumer_id == consumer.id))).scalars().all()
    changes = []
    for m in mappings:
        old = m.bsuid
        m.bsuid = ids.make_bsuid(consumer.country, consumer.id + ":" + new_phone, m.portfolio_id)
        changes.append((m.portfolio_id, old, m.bsuid))
    for portfolio_id, old, new in changes:
        numbers = (await session.execute(select(BusinessNumber).where(
            BusinessNumber.portfolio_id == portfolio_id))).scalars().all()
        for bn in numbers:
            await webhooks.emit_system_phone_change(session, bn, consumer, old, new)
    await session.commit()
    return {"id": consumer.id, "phone": consumer.phone,
            "bsuids": [{"portfolio_id": p, "old": o, "new": n} for p, o, n in changes]}


@router.post("/consumers/{consumer_id}/send_message")
async def consumer_send_message(consumer_id: str, body: dict,
                                ctx: SandboxContext = Depends(sandbox_auth)):
    session = ctx.session
    consumer = await _own_consumer(ctx, consumer_id)
    bn = await _own_number(ctx, body.get("to_business_number_id", ""))
    portfolio = await session.get(Portfolio, bn.portfolio_id)
    now = rules.now_for(ctx.tenant)
    wamid = await ids.new_wamid(session)
    session.add(Message(id=await ids.new_id(session, "msg"), wamid=wamid, direction="inbound",
                        business_number_id=bn.id, consumer_id=consumer.id, addressed_by="phone",
                        type="text", payload={"text": {"body": body.get("text", "")}},
                        statuses=[], created_at=now))
    await rules.open_window(session, bn, consumer, now)  # §9.3
    cfg = rules.effective_config(bn, ctx.tenant)
    if await rules.phone_visible(session, bn, consumer, portfolio, ctx.tenant, cfg):
        await rules.touch_contact(session, portfolio, bn, consumer, now)  # §6
    await webhooks.emit_inbound_message(session, bn, consumer, wamid,
                                        {"type": "text", "text": {"body": body.get("text", "")}})
    await session.commit()
    return {"wamid": wamid}


async def _emit_contacts_webhook(session, bn: BusinessNumber, consumer: Consumer,
                                 tenant: Tenant, origin: str) -> str:
    """§9.5 contacts webhook; vcard only when origin='other'."""
    portfolio = await session.get(Portfolio, bn.portfolio_id)
    cfg = rules.effective_config(bn, tenant)
    bsuid = await rules.get_or_create_bsuid(session, consumer, portfolio)
    parent = await rules.parent_id_for(session, consumer, portfolio, cfg, tenant)
    now = rules.now_for(tenant)
    wamid = await ids.new_wamid(session)
    shared = {"name": {"formatted_name": consumer.display_name,
                       "first_name": consumer.display_name.split(" ")[0]},
              "phones": [{"phone": f"+{consumer.phone}", "wa_id": consumer.phone,
                          "type": "MOBILE"}],
              "origin": origin}
    if origin == "other":
        shared["vcard"] = (f"BEGIN:VCARD\nVERSION:3.0\nFN:{consumer.display_name}\n"
                           f"TEL;TYPE=CELL:+{consumer.phone}\nEND:VCARD")
    visible = await rules.phone_visible(session, bn, consumer, portfolio, tenant, cfg)
    msg = {"id": wamid, "timestamp": str(int(now.timestamp())), "type": "contacts",
           "from_user_id": bsuid, "contacts": [shared]}
    if visible:
        msg["from"] = consumer.phone
    value = {"messaging_product": "whatsapp", "metadata": webhooks.metadata(bn),
             "contacts": [webhooks.build_contact(consumer, bsuid, visible=visible,
                                                 parent_user_id=parent)],
             "messages": [msg]}
    await webhooks.record_and_send(session, bn, tenant.id, "messages",
                                   webhooks.envelope(portfolio.id, "messages", value))
    if origin == "contact_request":
        # a contact_request share reveals the phone going forward (§9.5)
        await rules.touch_contact(session, portfolio, bn, consumer, now, phone_known=True)
    return wamid


@router.post("/consumers/{consumer_id}/tap_share_contact")
async def tap_share_contact(consumer_id: str, body: dict,
                            ctx: SandboxContext = Depends(sandbox_auth)):
    session = ctx.session
    consumer = await _own_consumer(ctx, consumer_id)
    wamid = body.get("message_wamid", "")
    msg = (await session.execute(select(Message).where(
        Message.wamid == wamid, Message.consumer_id == consumer.id,
        Message.direction == "outbound"))).scalar_one_or_none()
    if msg is None:
        raise ApiError(100, f"No outbound message with wamid '{wamid}' for this consumer.",
                       http_status=404)
    is_rci = (msg.type == "interactive"
              and (msg.payload.get("interactive") or {}).get("type") == "request_contact_info")
    if not is_rci and msg.type == "template":
        tpl = (msg.payload.get("template") or {})
        bn_row = await session.get(BusinessNumber, msg.business_number_id)
        t = (await session.execute(select(Template).where(
            Template.portfolio_id == bn_row.portfolio_id,
            Template.name == tpl.get("name")))).scalars().first()
        if t:
            is_rci = any(str(b.get("type", "")).upper() == "REQUEST_CONTACT_INFO"
                         for comp in (t.components or []) if str(comp.get("type", "")).upper() == "BUTTONS"
                         for b in comp.get("buttons", []))
    if not is_rci:
        raise ApiError(100, "message_wamid must reference a REQUEST_CONTACT_INFO "
                            "template or interactive message.")
    if not any(s.get("status") == "delivered" for s in (msg.statuses or [])):
        raise ApiError(100, "The referenced message has not been delivered yet.")
    bn = await session.get(BusinessNumber, msg.business_number_id)
    share_wamid = await _emit_contacts_webhook(session, bn, consumer, ctx.tenant,
                                               origin="contact_request")
    await session.commit()
    return {"wamid": share_wamid}


@router.post("/consumers/{consumer_id}/share_contact_manually")
async def share_contact_manually(consumer_id: str, body: dict,
                                 ctx: SandboxContext = Depends(sandbox_auth)):
    consumer = await _own_consumer(ctx, consumer_id)
    bn = await _own_number(ctx, body.get("to_business_number_id", ""))
    wamid = await _emit_contacts_webhook(ctx.session, bn, consumer, ctx.tenant, origin="other")
    await ctx.session.commit()
    return {"wamid": wamid}


@router.post("/messages/{wamid}/force_failed")
async def force_failed(wamid: str, body: dict | None = None,
                       ctx: SandboxContext = Depends(sandbox_auth)):
    """§9.2: force a 'failed' status webhook for a specific wamid."""
    session = ctx.session
    msg = (await session.execute(select(Message).where(
        Message.wamid == wamid, Message.direction == "outbound"))).scalar_one_or_none()
    if msg is None:
        raise ApiError(100, f"No outbound message with wamid '{wamid}'.", http_status=404)
    consumer = await session.get(Consumer, msg.consumer_id)
    if consumer.tenant_id != ctx.tenant.id:
        raise ApiError(100, f"No outbound message with wamid '{wamid}'.", http_status=404)
    code = (body or {}).get("code", 131049)
    await webhooks.emit_status(session, msg.id, "failed", failed_code=int(code))
    await session.commit()
    return {"wamid": wamid, "status": "failed", "code": code}


@router.delete("/state/consumers/{consumer_id}/contact_book/{portfolio_id}")
async def surgical_contact_book_delete(consumer_id: str, portfolio_id: str,
                                       ctx: SandboxContext = Depends(sandbox_auth)):
    """Debug scalpel: remove ONLY the contact book entry, leaving the 30-day
    cache untouched (unlike the Mock API DELETE /contact_book, which also
    clears the portfolio's caches). Lets testers stage cache-only visibility."""
    consumer = await _own_consumer(ctx, consumer_id)
    await _own_portfolio(ctx, portfolio_id)
    entry = (await ctx.session.execute(select(ContactBookEntry).where(
        ContactBookEntry.portfolio_id == portfolio_id,
        ContactBookEntry.consumer_id == consumer.id))).scalar_one_or_none()
    if entry:
        await ctx.session.delete(entry)
        await ctx.session.commit()
    return {"deleted": entry is not None}


# ----------------------------------------------------------- tenant & time

@router.patch("/tenants/me")
async def patch_tenant(body: dict, ctx: SandboxContext = Depends(sandbox_auth)):
    if "ga_mode" in body:
        ctx.tenant.ga_mode = bool(body["ga_mode"])
    if "status_delays_ms" in body:
        ctx.tenant.status_delays_ms = body["status_delays_ms"]
    await ctx.session.commit()
    return {"id": ctx.tenant.id, "ga_mode": ctx.tenant.ga_mode,
            "status_delays_ms": ctx.tenant.status_delays_ms,
            "clock_offset_s": ctx.tenant.clock_offset_s}


@router.post("/time/advance")
async def advance_time(body: dict, ctx: SandboxContext = Depends(sandbox_auth)):
    hours = body.get("hours")
    if not isinstance(hours, (int, float)) or hours < 0:
        raise ApiError(100, "Field 'hours' must be a non-negative number.")
    ctx.tenant.clock_offset_s += int(hours * 3600)
    await ctx.session.commit()
    return {"clock_offset_s": ctx.tenant.clock_offset_s}


# ----------------------------------------------------- parent BSUID accounts

@router.post("/parent_bsuid_accounts")
async def create_parent_account(body: dict, ctx: SandboxContext = Depends(sandbox_auth)):
    session = ctx.session
    portfolio_ids = body.get("portfolio_ids") or []
    if not portfolio_ids:
        raise ApiError(100, "Field 'portfolio_ids' must list at least one portfolio.")
    portfolios = [await _own_portfolio(ctx, pid) for pid in portfolio_ids]
    account = ParentAccount(id=await ids.new_id(session, "pa"), tenant_id=ctx.tenant.id)
    session.add(account)
    await session.flush()
    for p in portfolios:
        existing = (await session.execute(select(ParentEnrollment).where(
            ParentEnrollment.portfolio_id == p.id))).scalar_one_or_none()
        if existing:
            raise ApiError(100, f"Portfolio '{p.id}' is already enrolled in a parent account.")
        session.add(ParentEnrollment(id=await ids.new_id(session, "pe"),
                                     parent_account_id=account.id, portfolio_id=p.id))
        p.parent_bsuid_enabled = True
    await session.commit()
    return {"parent_bsuid_account_id": account.id,
            "enrolled_business_portfolios": portfolio_ids}


# ------------------------------------------------------------- observability

@router.get("/webhook_deliveries")
async def webhook_deliveries(page: int = 1, page_size: int = 50,
                             business_number_id: str | None = None,
                             field: str | None = None,
                             ctx: SandboxContext = Depends(sandbox_auth)):
    q = select(WebhookDelivery).where(WebhookDelivery.tenant_id == ctx.tenant.id)
    if business_number_id:
        q = q.where(WebhookDelivery.business_number_id == business_number_id)
    if field:
        q = q.where(WebhookDelivery.field == field)
    total = (await ctx.session.execute(
        select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await ctx.session.execute(
        q.order_by(WebhookDelivery.id).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {"page": page, "page_size": page_size, "total": total,
            "data": [{"id": r.id, "business_number_id": r.business_number_id,
                      "endpoint": r.endpoint, "field": r.field, "payload": r.payload,
                      "attempts": r.attempts, "status": r.status,
                      "response_code": r.response_code,
                      "created_at": r.created_at.isoformat()} for r in rows]}


@router.get("/state/consumers/{consumer_id}")
async def consumer_state(consumer_id: str, ctx: SandboxContext = Depends(sandbox_auth)):
    session = ctx.session
    consumer = await _own_consumer(ctx, consumer_id)
    now = rules.now_for(ctx.tenant)
    mappings = (await session.execute(select(BsuidMapping).where(
        BsuidMapping.consumer_id == consumer.id))).scalars().all()
    parents = (await session.execute(select(ParentBsuid).where(
        ParentBsuid.consumer_id == consumer.id))).scalars().all()
    book = (await session.execute(select(ContactBookEntry).where(
        ContactBookEntry.consumer_id == consumer.id))).scalars().all()
    caches = (await session.execute(select(CacheEntry).where(
        CacheEntry.consumer_id == consumer.id))).scalars().all()
    windows = (await session.execute(select(ServiceWindow).where(
        ServiceWindow.consumer_id == consumer.id))).scalars().all()
    from datetime import timedelta
    return {
        "consumer": {"id": consumer.id, "phone": consumer.phone,
                     "display_name": consumer.display_name,
                     "username": consumer.username, "country": consumer.country},
        "bsuids": [{"portfolio_id": m.portfolio_id, "bsuid": m.bsuid} for m in mappings],
        "parent_bsuids": [{"parent_account_id": p.parent_account_id, "value": p.value}
                          for p in parents],
        "contact_book": [{"portfolio_id": b.portfolio_id, "phone_known": b.phone_known,
                          "created_at": b.created_at.isoformat()} for b in book],
        "cache": [{"business_number_id": c.business_number_id, "phone_known": c.phone_known,
                   "last_interaction_at": c.last_interaction_at.isoformat(),
                   "expires_at": (c.last_interaction_at + timedelta(days=30)).isoformat(),
                   "expired": c.last_interaction_at < now - timedelta(days=30)}
                  for c in caches],
        "service_windows": [{"business_number_id": w.business_number_id,
                             "opened_at": w.opened_at.isoformat(),
                             "open": w.opened_at > now - timedelta(hours=24)}
                            for w in windows],
        "tenant_now": now.isoformat(),
    }
