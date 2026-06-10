"""The three sandbox-only endpoints (§3). Everything else in this service is
the real API surface."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app import ids, rules, settings
from app.db import get_session
from app.deps import Ctx, auth
from app.errors import ApiError
from app.models import ApiKey, Bsuid, UserState, WebhookDelivery

router = APIRouter(tags=["Sandbox"])


@router.post(
    "/sandbox/keys",
    summary="Create a sandbox API key (start here)",
    description=(
        "Creates a virtual business phone number with its own implicit portfolio "
        "and one simulated consumer counterpart, and returns the `D360-API-KEY` "
        "to use on every other endpoint.\n\n"
        "No auth. Body fields (all optional): `name` (label), `country` "
        "(ISO alpha-2, default `BR` — sets the business and simulated user's "
        "country).\n\n**Errors**: `429` when more than "
        f"{settings.KEYS_PER_IP_PER_HOUR} keys are created from one IP within "
        "an hour. Keys auto-expire (default 90 days)."
    ),
    openapi_extra={"requestBody": {"required": False, "content": {"application/json": {
        "example": {"name": "my-test", "country": "BR"}}}}},
    responses={200: {"description": "The new key and its virtual number identity",
                     "content": {"application/json": {"example": {
                         "d360_api_key": "sk_sandbox_2f5a...",
                         "display_phone_number": "5511900000042",
                         "waba_id": "100000000000042",
                         "phone_number_id": "200000000000042",
                         "docs": "https://sandbox.example/docs",
                         "expires_at": "2026-09-09T00:00:00Z"}}}}})
async def create_key(request: Request, body: dict | None = None,
                     session=Depends(get_session)):
    body = body or {}
    ip = request.client.host if request.client else "unknown"
    hour_ago = rules.now() - timedelta(hours=1)
    recent = (await session.execute(select(func.count()).select_from(ApiKey).where(
        ApiKey.created_ip == ip, ApiKey.created_at > hour_ago))).scalar_one()
    if recent >= settings.KEYS_PER_IP_PER_HOUR:
        raise ApiError(429, f"Rate limit: max {settings.KEYS_PER_IP_PER_HOUR} sandbox keys "
                            "per IP per hour.", http_status=429)

    country = str(body.get("country", "BR")).upper()
    n = await ids.next_seq(session, "key")
    key_id = f"key_{n:06d}"
    when = rules.now()
    key = ApiKey(id=key_id, name=str(body.get("name", "sandbox"))[:64],
                 d360_api_key=ids.make_api_key(n),
                 display_phone_number=ids.business_phone(country, n),
                 waba_id=ids.waba_id(n), phone_number_id=ids.phone_number_id(n),
                 portfolio_id=ids.portfolio_id(n), config={}, created_ip=ip,
                 created_at=when, expires_at=when + timedelta(days=settings.KEY_TTL_DAYS))
    session.add(key)
    await session.flush()
    phone = ids.user_phone(country, key_id)
    user = UserState(api_key_id=key_id, phone=phone,
                     display_name=f"Sandbox User {n}", country=country,
                     bsuid=ids.make_bsuid(country, key_id, phone),
                     parent_bsuid=ids.make_parent_bsuid(country, key_id),
                     window_opened_at=when)  # the user "opened" the conversation
    session.add(user)
    session.add(Bsuid(id=await ids.new_id(session, "bs"), api_key_id=key_id,
                      value=user.bsuid, status="active"))
    await session.commit()
    base = str(request.base_url).rstrip("/")
    return {"d360_api_key": key.d360_api_key,
            "display_phone_number": key.display_phone_number,
            "waba_id": key.waba_id, "phone_number_id": key.phone_number_id,
            "docs": f"{base}/docs",
            "expires_at": key.expires_at.isoformat() + "Z"}


@router.put(
    "/sandbox/webhook",
    summary="Set the webhook endpoint for this key",
    description=(
        "Points all webhooks (inbound messages, statuses, contacts, "
        "`business_username_update`, system) at your URL. Optional `secret` "
        "enables HMAC signing: `X-Sandbox-Signature: sha256=<HMAC-SHA256 of body>`.\n\n"
        "**Errors**: `100` when `url` is missing."
    ),
    openapi_extra={"requestBody": {"content": {"application/json": {"example": {
        "url": "https://my-endpoint.example.com/webhooks", "secret": "optional-hmac-secret"}}}}},
    responses={200: {"content": {"application/json": {"example": {
        "url": "https://my-endpoint.example.com/webhooks", "secret_set": True}}}}})
async def set_webhook(body: dict, ctx: Ctx = Depends(auth)):
    url = body.get("url")
    if not url:
        raise ApiError(100, "Field 'url' is required.")
    ctx.key.webhook_url = url
    ctx.key.webhook_secret = body.get("secret")
    await ctx.session.commit()
    return {"url": ctx.key.webhook_url, "secret_set": ctx.key.webhook_secret is not None}


@router.get(
    "/sandbox/webhook",
    summary="Read webhook config + last 50 delivery attempts",
    description=(
        "Returns the configured URL and the **last 50 webhook delivery "
        "attempts** (payload, status, response code, timestamp) — your main "
        "debugging tool when your endpoint isn't reachable. Webhooks are "
        "recorded here even when no URL is configured (status `logged`)."
    ),
    responses={200: {"content": {"application/json": {"example": {
        "url": "https://my-endpoint.example.com/webhooks", "secret_set": False,
        "deliveries": [{"field": "messages", "status": "delivered", "attempts": 1,
                        "response_code": 200, "created_at": "2026-06-09T12:00:00",
                        "payload": {"object": "whatsapp_business_account",
                                    "entry": ["..."]}}]}}}}})
async def get_webhook(ctx: Ctx = Depends(auth)):
    rows = (await ctx.session.execute(
        select(WebhookDelivery).where(WebhookDelivery.api_key_id == ctx.key.id)
        .order_by(WebhookDelivery.id.desc()).limit(50))).scalars().all()
    return {"url": ctx.key.webhook_url, "secret_set": ctx.key.webhook_secret is not None,
            "deliveries": [{"id": r.id, "field": r.field, "status": r.status,
                            "attempts": r.attempts, "response_code": r.response_code,
                            "created_at": r.created_at.isoformat(),
                            "payload": r.payload} for r in rows]}


@router.put(
    "/sandbox/config",
    summary="Configure the behavior you want to see",
    description=(
        "The single behavior switchboard: platform stage (`ga_mode`), the "
        "simulated end-user (`user`), automatic consumer behavior "
        "(`consumer_actions`), status webhooks (`statuses`) and error "
        "injection (`inject_error`). Partial updates **merge**; `\"auto\"` "
        "always means *apply the real production rules*. Changes apply to the "
        "next API call. Returns the full active config.\n\n"
        "**Errors**: `100` for unknown keys or invalid enum values."
    ),
    openapi_extra={"requestBody": {"content": {"application/json": {"example": {
        "ga_mode": True,
        "user": {"has_username": True, "phone_visibility": "never"},
        "consumer_actions": {"reply_to_messages": True, "reply_delay_ms": 1000},
        "statuses": {"sequence": ["sent", "delivered", "read"], "delays_ms": [0, 500, 1000]}
    }}}}},
    responses={200: {"description": "The full active config after the merge",
                     "content": {"application/json": {"example": rules.CONFIG_DEFAULTS}}}})
async def set_config(body: dict, ctx: Ctx = Depends(auth)):
    rules.validate_config_patch(body)
    ctx.key.config = rules.deep_merge(ctx.key.config or {}, body)
    await ctx.session.commit()
    return rules.effective_config(ctx.key)


@router.get(
    "/sandbox/config",
    summary="Read the active config",
    description="Returns the full active config (defaults merged with everything you set).",
    responses={200: {"content": {"application/json": {"example": rules.CONFIG_DEFAULTS}}}})
async def get_config(ctx: Ctx = Depends(auth)):
    return rules.effective_config(ctx.key)
