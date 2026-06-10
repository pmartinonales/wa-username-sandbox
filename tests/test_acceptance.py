"""Acceptance scenarios (spec v2 §6 = v1 §12 re-expressed against the v2
surface: no Simulation API — config + consumer_actions only)."""
import asyncio
import socket
from datetime import timedelta

from sqlalchemy import update

from tests.conftest import drain, inbound_of, make_env, statuses_of

BSUID = "BR.13491208655302741918"
RCI_INTERACTIVE = {"type": "interactive",
                   "interactive": {"type": "request_contact_info",
                                   "body": {"text": "Share?"},
                                   "action": {"name": "request_contact_info"}}}


# 1 ─ golden path
async def test_golden_path(client):
    env = await make_env(client, config={
        "user": {"has_username": True},
        "consumer_actions": {"reply_to_messages": True}})
    r = await env.send({"recipient": BSUID, "type": "text", "text": {"body": "hi"}})
    assert r["contacts"][0] == {"input": BSUID, "user_id": BSUID}
    wamid = r["messages"][0]["id"]
    assert wamid.startswith("wamid.")

    values = await env.values()
    pairs = [s for v, s in statuses_of(values) if s["id"] == wamid]
    assert [s["status"] for s in pairs] == ["sent", "delivered", "read"]
    for s in pairs:
        assert s["recipient_user_id"] == BSUID
        assert "recipient_id" not in s
    inbound = inbound_of(values)
    assert inbound, "no auto-reply received"
    msg, contact = inbound[-1]["messages"][0], inbound[-1]["contacts"][0]
    assert "from" not in msg and "wa_id" not in contact
    assert msg["from_user_id"] == BSUID
    assert contact["profile"]["username"]

    # REQUEST_CONTACT_INFO → tap → contacts webhook → phone visible again
    await env.send({"recipient": BSUID, **RCI_INTERACTIVE})
    await drain()
    shares = [v for v in await env.values()
              if "messages" in v and v["messages"][0]["type"] == "contacts"]
    assert shares
    share = shares[-1]["messages"][0]["contacts"][0]
    assert share["origin"] == "contact_request"
    assert "vcard" not in share
    assert share["phones"][0]["wa_id"]

    r = await env.send({"recipient": BSUID, "type": "text", "text": {"body": "ty"}})
    last = [s for v, s in statuses_of(await env.values())
            if s["id"] == r["messages"][0]["id"]]
    assert all(s.get("recipient_id") for s in last), "phone did not come back"


# 2 ─ closed window → 131047; open → succeeds
async def test_closed_window(client):
    env = await make_env(client, config={"user": {"service_window": "closed"}})
    r = await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}},
                       expect=400)
    assert r["error"]["code"] == 131047
    assert "24-hour" in r["error"]["error_data"]["details"]
    await env.config({"user": {"service_window": "open"}})
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})


# 3 ─ auth template to BSUID → 131062; to phone → ok
async def test_auth_template_to_bsuid(client):
    env = await make_env(client)
    await env.call("POST", "/v1/configs/templates", {
        "name": "otp", "language": "en", "category": "utility",
        "auth_flavor": "one-tap", "components": []})
    body = {"type": "template", "template": {"name": "otp", "language": {"code": "en"}}}
    r = await env.send({"recipient": BSUID, **body}, expect=400)
    assert r["error"]["code"] == 131062
    r = await env.send({"to": "5511988880001", **body})
    assert r["messages"][0]["id"]


# 4 ─ BSUID from key A used with key B → 131009
async def test_foreign_bsuid(client):
    env_a = await make_env(client)
    env_b = await make_env(client)
    # the sandbox-GENERATED bsuid of A's user (from a status webhook) is
    # portfolio-scoped: using it with B's key is the cross-portfolio mistake
    await env_a.send({"to": "5511911110001", "type": "text", "text": {"body": "x"}})
    bsuid_a = statuses_of(await env_a.values())[-1][1]["recipient_user_id"]
    r = await env_b.send({"recipient": bsuid_a, "type": "text", "text": {"body": "x"}},
                         expect=400)
    assert r["error"]["code"] == 131009
    assert "portfolio" in r["error"]["error_data"]["details"]


async def test_supplied_bsuid_shared_across_keys(client):
    # tester-INVENTED values (e.g. the docs example BSUID) are adoptable by
    # many keys — BSUIDs are portfolio-scoped, so this is not a conflict
    env_a = await make_env(client)
    env_b = await make_env(client)
    r = await env_a.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    assert r["contacts"][0]["user_id"] == BSUID
    r = await env_b.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    assert r["contacts"][0]["user_id"] == BSUID


async def test_malformed_bsuid(client):
    env = await make_env(client)
    for bad in ("13491208655302741918", "BR-13491208655302741918", "BR.12345",
                "br.13491208655302741918", "BR.abc91208655302741918"):
        r = await env.send({"recipient": bad, "type": "text", "text": {"body": "x"}},
                           expect=400)
        assert r["error"]["code"] == 131009, bad


# 5 ─ to + recipient both present → phone wins
async def test_phone_precedence(client):
    env = await make_env(client)
    r = await env.send({"to": "5511988880001", "recipient": BSUID,
                        "type": "text", "text": {"body": "x"}})
    c = r["contacts"][0]
    assert c["wa_id"] == "5511988880001"
    assert "user_id" not in c
    assert r["messages"][0]["id"].startswith("wamid.")


# 6 ─ ga_mode=false: phone everywhere despite username
async def test_pre_ga(client):
    env = await make_env(client, config={
        "ga_mode": False, "user": {"has_username": True},
        "consumer_actions": {"reply_to_messages": True}})
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    values = await env.values()
    for v, s in statuses_of(values):
        assert s.get("recipient_id"), s
        assert v["contacts"][0].get("wa_id")
    inbound = inbound_of(values)
    assert inbound and inbound[-1]["messages"][0].get("from")


# 7 ─ contact book delete → BSUID-only; inbound still arrives
async def test_contact_book_delete(client):
    env = await make_env(client, config={
        "consumer_actions": {"reply_to_messages": True}})
    # phone send writes the contact book → phone visible
    await env.send({"to": "5511988880001", "type": "text", "text": {"body": "x"}})
    values = await env.values()
    bsuid = statuses_of(values)[-1][1]["recipient_user_id"]
    assert statuses_of(values)[-1][1].get("recipient_id")

    r = await env.call("DELETE", f"/contact_book?messaging_product=whatsapp&bsuid={bsuid}")
    assert r == {"messaging_product": "whatsapp", "success": True, "deleted": True}
    r = await env.call("DELETE", f"/contact_book?messaging_product=whatsapp&bsuid={bsuid}")
    assert r["success"] is True and r["deleted"] is False

    before = len(await env.values())
    await env.send({"recipient": bsuid, "type": "text", "text": {"body": "still?"}})
    values = (await env.values())[before:]
    for v, s in statuses_of(values):
        assert "recipient_id" not in s, s
    inbound = inbound_of(values)
    assert inbound, "inbound dropped after contact book delete (alpha bug regression)"
    assert "from" not in inbound[-1]["messages"][0]

    # rejections: parent BSUID and foreign BSUID → success:false, 400
    r = await env.call("DELETE", "/contact_book?messaging_product=whatsapp&bsuid=BR.ENT.123456",
                       expect=400)
    assert r["success"] is False
    other = await make_env(client)
    await other.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    r = await env.call("DELETE", f"/contact_book?messaging_product=whatsapp&bsuid={BSUID}",
                       expect=400)
    assert r["success"] is False


# 8 ─ cache vs contact book: forced in_contact_book=false, cache still works
async def test_cache_independent_of_contact_book(client):
    env = await make_env(client, config={"user": {"in_contact_book": False}})
    await env.send({"to": "5511988880001", "type": "text", "text": {"body": "x"}})
    s = statuses_of(await env.values())[-1][1]
    assert s.get("recipient_id"), "30-day cache should keep the phone visible"
    # a fresh key with no phone history has no cache → not visible
    env2 = await make_env(client, config={"user": {"in_contact_book": False,
                                                   "service_window": "open"}})
    await env2.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    s = statuses_of(await env2.values())[-1][1]
    assert "recipient_id" not in s


# 9 ─ username: rate limit, duplicate, bad format, lifecycle
async def test_username_rules(client):
    env = await make_env(client, name="acme")
    sugg = await env.call("GET", "/username_suggestions")
    assert len(sugg["data"][0]["username_suggestions"]) == 3

    for i in range(3):
        r = await env.call("POST", "/username", {"username": f"acme.try{i}"})
        assert r["status"] == "approved"  # ga_mode defaults to true
    r = await env.call("POST", "/username", {"username": "acme.try9"}, expect=429)
    assert r["error"]["code"] == 131056
    assert "Next change available at" in r["error"]["error_data"]["details"]
    assert "claimed" not in r["error"]["error_data"]["details"].lower()

    env2 = await make_env(client)
    r = await env2.call("POST", "/username", {"username": "ACME.TRY2"}, expect=400)
    assert r["error"]["code"] == 147001  # case-insensitive duplicate

    for bad in ("ab", "a" * 36, "no letter!", "12345678", ".lead", "trail.",
                "dou..ble", "wwwshop", "acme.com", "acme.html"):
        r = await env2.call("POST", "/username", {"username": bad}, expect=400)
        assert r["error"]["code"] == 100, bad

    r = await env.call("GET", "/username")
    assert r == {"username": "acme.try2", "status": "active"}  # active under GA
    assert await env.call("DELETE", "/username") == {"success": True}
    assert await env.call("GET", "/username") == {}
    assert await env.call("DELETE", "/username") == {"success": False}
    ups = await env.values(field="business_username_update")
    assert [u["status"] for u in ups] == ["approved", "approved", "approved", "deleted"]
    assert "username" not in ups[-1]
    assert ups[0]["username"] == "acme.try0"

    env3 = await make_env(client, config={"ga_mode": False})
    r = await env3.call("POST", "/username", {"username": "preg.a.name"})
    assert r["status"] == "reserved"
    assert (await env3.call("GET", "/username"))["status"] == "reserved"


# 10 ─ template button validation + interactive typo
async def test_template_validation(client):
    env = await make_env(client)
    r = await env.call("POST", "/v1/configs/templates", {
        "name": "t1", "language": "en", "category": "utility",
        "components": [{"type": "BUTTONS", "buttons": [
            {"type": "REQUEST_CONTACT_INFO", "text": "Share My Info"}]}]}, expect=400)
    assert r["error"]["error_subcode"] == 2388153
    assert r["error"]["error_user_title"] == "Button text modification not allowed"
    r = await env.call("POST", "/v1/configs/templates", {
        "name": "t2", "language": "en", "category": "utility",
        "components": [{"type": "BUTTONS", "buttons": [
            {"type": "REQUEST_CONTACT_INFO"}]}]}, expect=400)
    assert r["error"]["error_subcode"] == 2388050
    r = await env.send({"recipient": BSUID, "type": "interactive",
                        "interactive": {"type": "contact_request"}}, expect=400)
    assert "request_contact_info" in r["error"]["error_data"]["details"]


# 11 ─ phone change regenerates BSUID + system webhook; old BSUID → 131009
async def test_phone_change(client):
    env = await make_env(client)
    await env.send({"to": "5511911110001", "type": "text", "text": {"body": "x"}})
    old_bsuid = statuses_of(await env.values())[-1][1]["recipient_user_id"]
    await env.send({"to": "5511922220002", "type": "text", "text": {"body": "x"}})
    await drain()
    systems = [v for v in await env.values()
               if "messages" in v and v["messages"][0]["type"] == "system"]
    assert systems, "no system webhook on phone change"
    sysmsg = systems[-1]["messages"][0]["system"]
    assert sysmsg["body"] == "User changed phone number"
    assert sysmsg["user_id"] == old_bsuid
    new_bsuid = sysmsg["new_user_id"]
    assert new_bsuid != old_bsuid
    r = await env.send({"recipient": old_bsuid, "type": "text", "text": {"body": "x"}},
                       expect=400)
    assert r["error"]["code"] == 131009
    await env.send({"recipient": new_bsuid, "type": "text", "text": {"body": "x"}})


# 12 ─ failed status: no contacts; phone-addressed → no recipient_user_id
async def test_failed_status_shape(client):
    env = await make_env(client, config={
        "statuses": {"sequence": ["sent", "failed"], "delays_ms": [0, 0]}})
    await env.send({"to": "5511988880001", "type": "text", "text": {"body": "x"}})
    failed = [v for v, s in statuses_of(await env.values()) if s["status"] == "failed"]
    assert failed
    v = failed[-1]
    assert "contacts" not in v
    s = v["statuses"][0]
    assert s["recipient_id"] and "recipient_user_id" not in s
    assert s["errors"][0]["code"] == 131049

    env2 = await make_env(client, config={
        "statuses": {"sequence": ["sent", "failed"], "delays_ms": [0, 0],
                     "failed_error_code": 131026}})
    await env2.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    v = [v for v, s in statuses_of(await env2.values()) if s["status"] == "failed"][-1]
    s = v["statuses"][0]
    assert "contacts" not in v
    assert s["recipient_user_id"] and "recipient_id" not in s
    assert s["errors"][0]["code"] == 131026


# 13 ─ key isolation
async def test_key_isolation(client):
    env_a = await make_env(client)
    env_b = await make_env(client)
    await env_a.send({"to": "5511988880001", "type": "text", "text": {"body": "x"}})
    assert await env_b.values() == []
    assert (await env_a.values()) != []
    r = await client.post("/messages", json={"to": "1", "type": "text",
                                             "text": {"body": "x"}},
                          headers={"D360-API-KEY": "sk_sandbox_nope"})
    assert r.status_code == 401


# 14 ─ config-first flow, visibility flip, error injection
async def test_config_first(client):
    env = await make_env(client, config={
        "ga_mode": True, "user": {"has_username": True, "phone_visibility": "never"}})
    r = await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    assert r["contacts"][0]["user_id"] == BSUID
    values = await env.values()
    for v, s in statuses_of(values):
        assert "recipient_id" not in s
        if s["status"] in ("delivered", "read"):
            assert v["contacts"][0]["profile"]["username"]
        if s["status"] == "sent":
            assert "username" not in v["contacts"][0]["profile"]

    await env.config({"user": {"phone_visibility": "always"}})
    before = len(await env.values())
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    for v, s in statuses_of((await env.values())[before:]):
        assert s.get("recipient_id"), "phone_visibility=always not honored"

    await env.config({"inject_error": {"on": "messages", "code": 131047, "times": 1}})
    r = await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}},
                       expect=400)
    assert r["error"]["code"] == 131047
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    assert (await env.call("GET", "/sandbox/config"))["inject_error"] is None


# 15 ─ the five-minute path, with a real HTTP webhook receiver
async def test_five_minute_path(client):
    import json as _json

    import uvicorn

    received: list[dict] = []

    async def receiver(scope, receive, send):
        body = b""
        while True:
            ev = await receive()
            body += ev.get("body", b"")
            if not ev.get("more_body"):
                break
        received.append(_json.loads(body))
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
        assert server.started

        # 1. POST /sandbox/keys
        r = await client.post("/sandbox/keys", json={"name": "fivemin"})
        key = r.json()["d360_api_key"]
        hk = {"D360-API-KEY": key}
        # 2. PUT /sandbox/webhook
        r = await client.put("/sandbox/webhook", headers=hk,
                             json={"url": f"http://127.0.0.1:{port}/wh"})
        assert r.status_code == 200
        # 3. PUT /sandbox/config
        r = await client.put("/sandbox/config", headers=hk, json={
            "user": {"has_username": True, "phone_visibility": "never"},
            "consumer_actions": {"reply_to_messages": True, "reply_delay_ms": 0},
            "statuses": {"delays_ms": [0, 0, 0]}})
        assert r.status_code == 200
        # 4. POST /messages — the only other endpoints used are real API.
        # BSUID-addressed: phone-addressed statuses would always carry the
        # phone per Meta's identifier quick reference.
        r = await client.post("/messages", headers=hk, json={
            "recipient": "BR.13491208655302741918", "type": "text",
            "text": {"body": "Hi!"}})
        assert r.status_code == 200, r.text
        wamid = r.json()["messages"][0]["id"]
        assert r.json()["contacts"][0]["user_id"] == "BR.13491208655302741918"

        await drain()
        values = [p["entry"][0]["changes"][0]["value"] for p in received]
        sts = [s for v in values for s in v.get("statuses", [])]
        assert {s["status"] for s in sts} == {"sent", "delivered", "read"}
        for s in sts:
            assert s["id"] == wamid
            assert s["recipient_user_id"] and "recipient_id" not in s
        inbound = [v for v in values if "messages" in v and "statuses" not in v]
        assert inbound, "no inbound reply delivered to the webhook endpoint"
        msg = inbound[-1]["messages"][0]
        assert msg["from_user_id"] and "from" not in msg
    finally:
        server.should_exit = True
        await task


# 16 ─ surface audit: exactly three non-Meta endpoints
async def test_surface_audit(client):
    r = await client.get("/openapi.json")
    paths = set(r.json()["paths"])
    sandbox_paths = {p for p in paths if p.startswith("/sandbox")}
    assert sandbox_paths == {"/sandbox/keys", "/sandbox/webhook", "/sandbox/config"}
    assert paths - sandbox_paths == {
        "/messages", "/marketing_messages", "/username", "/username_suggestions",
        "/contact_book", "/parent-bsuid-accounts", "/v1/configs/templates"}


async def test_marketing_messages(client):
    env = await make_env(client)
    await env.call("POST", "/v1/configs/templates",
                   {"name": "promo", "language": "en", "category": "marketing",
                    "components": []})
    await env.call("POST", "/v1/configs/templates",
                   {"name": "util", "language": "en", "category": "utility",
                    "components": []})
    r = await env.call("POST", "/marketing_messages",
                       {"to": "5511988880001", "type": "template",
                        "template": {"name": "promo", "language": {"code": "en"}}})
    assert r["messages"][0]["message_status"] == "accepted"
    r = await env.call("POST", "/marketing_messages",
                       {"to": "5511988880001", "type": "template",
                        "template": {"name": "util", "language": {"code": "en"}}},
                       expect=400)
    assert r["error"]["code"] == 100
    r = await env.call("POST", "/marketing_messages",
                       {"to": "5511988880001", "type": "text", "text": {"body": "x"}},
                       expect=400)
    assert r["error"]["code"] == 100


async def test_parent_bsuid(client):
    env = await make_env(client, config={"user": {"parent_bsuid": True}})
    acct = await env.call("GET", "/parent-bsuid-accounts")
    assert acct["parent_bsuid_account_id"]
    assert len(acct["enrolled_business_portfolios"]) == 1
    await env.send({"recipient": BSUID, "type": "text", "text": {"body": "x"}})
    for v, s in statuses_of(await env.values()):
        assert ".ENT." in s["recipient_parent_user_id"]
        assert v["contacts"][0]["parent_user_id"] == s["recipient_parent_user_id"]
    parent = statuses_of(await env.values())[-1][1]["recipient_parent_user_id"]
    r = await env.send({"recipient": parent, "type": "text", "text": {"body": "x"}})
    assert r["contacts"][0]["user_id"] == parent
    # disabled → empty account + 131009 on parent sends
    env2 = await make_env(client)
    acct = await env2.call("GET", "/parent-bsuid-accounts")
    assert acct == {"parent_bsuid_account_id": None, "enrolled_business_portfolios": []}
    r = await env2.send({"recipient": "BR.ENT.123456789012345", "type": "text",
                         "text": {"body": "x"}}, expect=400)
    assert r["error"]["code"] == 131009


async def test_key_hygiene(client, monkeypatch):
    from app import settings as s
    from app.db import SessionLocal
    from app.models import ApiKey
    from app.rules import now

    env = await make_env(client)
    async with SessionLocal() as session:
        await session.execute(update(ApiKey).values(
            expires_at=now() - timedelta(days=1)))
        await session.commit()
    r = await client.post("/messages", headers=env.hk,
                          json={"to": "1", "type": "text", "text": {"body": "x"}})
    assert r.status_code == 401
    assert "expired" in r.json()["error"]["error_data"]["details"]

    monkeypatch.setattr(s, "KEYS_PER_IP_PER_HOUR", 2)
    assert (await client.post("/sandbox/keys")).status_code == 200
    assert (await client.post("/sandbox/keys")).status_code == 429


async def test_config_validation(client):
    env = await make_env(client)
    for bad in ({"nope": 1},
                {"user": {"phone_visibility": "sometimes"}},
                {"user": {"in_contact_book": "yes"}},
                {"statuses": {"sequence": ["sent", "exploded"]}},
                {"inject_error": {"code": 1}}):
        r = await env.config(bad, expect=400)
        assert r["error"]["code"] == 100, bad
    # partial updates merge
    await env.config({"user": {"phone_visibility": "never"}})
    cfg = await env.call("GET", "/sandbox/config")
    assert cfg["user"]["phone_visibility"] == "never"
    assert cfg["user"]["has_username"] is True  # untouched default
    assert cfg["statuses"]["delays_ms"] == [0, 0, 0]  # earlier FAST patch kept
