"""Table-driven tests for the identifier-inclusion matrix (v1 §9.4)."""
import pytest

from tests.conftest import drain, inbound_of, make_env, statuses_of

BSUID = "BR.13491208655302741918"

# (has_username, phone_visible) → expectations for inbound webhooks
INBOUND_CASES = [
    # username adopted, phone hidden → BSUID-only + username
    ({"has_username": True, "phone_visibility": "never"},
     dict(phone=False, username=True)),
    # username adopted, phone visible → everything
    ({"has_username": True, "phone_visibility": "always"},
     dict(phone=True, username=True)),
    # no username → phone always, never a username field
    ({"has_username": False, "phone_visibility": "auto"},
     dict(phone=True, username=False)),
]


@pytest.mark.parametrize("user_cfg,expect", INBOUND_CASES)
async def test_inbound_matrix(client, user_cfg, expect):
    env = await make_env(client, config={
        "user": user_cfg, "consumer_actions": {"reply_to_messages": True}})
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    await drain()
    inbound = inbound_of(await env.values())
    assert inbound
    msg, contact = inbound[-1]["messages"][0], inbound[-1]["contacts"][0]
    # BSUIDs always present
    assert msg["from_user_id"] and contact["user_id"]
    assert ("from" in msg) is expect["phone"]
    assert ("wa_id" in contact) is expect["phone"]
    assert ("username" in contact["profile"]) is expect["username"]


# statuses: (addressed_by, phone_visible) → recipient_id expectations
async def test_status_matrix_phone_addressed(client):
    # phone-addressed under auto rules: wa_id/recipient_id always, even when the
    # real rules would hide the phone (GA + username + no history)
    env = await make_env(client, config={"user": {"has_username": True,
                                                  "phone_visibility": "auto",
                                                  "in_contact_book": False}})
    await env.send({"to": "5511988880001", "type": "text", "text": {"body": "x"}})
    for v, s in statuses_of(await env.values()):
        assert s["recipient_id"] == "5511988880001"
        assert s["recipient_user_id"]
        assert v["contacts"][0]["wa_id"]
        assert v["contacts"][0]["user_id"]


async def test_status_matrix_forced_never_wins_over_phone_addressed(client):
    # a forced phone_visibility="never" strips the phone even on phone sends
    # (this is what makes the five-minute path BSUID-only)
    env = await make_env(client, config={"user": {"phone_visibility": "never"}})
    await env.send({"to": "5511988880001", "type": "text", "text": {"body": "x"}})
    for v, s in statuses_of(await env.values()):
        assert "recipient_id" not in s
        assert s["recipient_user_id"]
        assert "wa_id" not in v["contacts"][0]


async def test_status_matrix_bsuid_addressed_hidden(client):
    env = await make_env(client, config={"user": {"phone_visibility": "never"}})
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    for v, s in statuses_of(await env.values()):
        assert "recipient_id" not in s
        assert s["recipient_user_id"] == BSUID
        assert "wa_id" not in v["contacts"][0]


async def test_status_matrix_bsuid_addressed_visible(client):
    env = await make_env(client, config={"user": {"phone_visibility": "always"}})
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    for v, s in statuses_of(await env.values()):
        assert s["recipient_id"]
        assert s["recipient_user_id"] == BSUID


async def test_status_username_only_on_delivered_read(client):
    env = await make_env(client, config={"user": {"has_username": True}})
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    seen = set()
    for v, s in statuses_of(await env.values()):
        has = "username" in v["contacts"][0]["profile"]
        assert has is (s["status"] in ("delivered", "read")), s["status"]
        seen.add(s["status"])
    assert seen == {"sent", "delivered", "read"}


async def test_webhook_hmac_signature(client):
    """Webhook bodies are signed with the configured secret."""
    import asyncio
    import hashlib
    import hmac
    import json as _json
    import socket

    import uvicorn

    captured: list[tuple[bytes, str | None]] = []

    async def receiver(scope, receive, send):
        body = b""
        while True:
            ev = await receive()
            body += ev.get("body", b"")
            if not ev.get("more_body"):
                break
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        captured.append((body, headers.get("x-sandbox-signature")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(receiver, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        env = await make_env(client)
        await env.call("PUT", "/sandbox/webhook",
                       {"url": f"http://127.0.0.1:{port}/wh", "secret": "s3cret"})
        await env.send({"to": "5511988880001", "type": "text", "text": {"body": "x"}})
        await drain()
        assert captured
        for body, sig in captured:
            expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
            assert sig == f"sha256={expected}"
            _json.loads(body)  # valid JSON envelope
        deliveries = await env.deliveries()
        assert all(d["status"] == "delivered" and d["response_code"] == 200
                   for d in deliveries)
    finally:
        server.should_exit = True
        await task
