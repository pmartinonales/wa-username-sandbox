"""Behavioral rules from §3, §6–§9 not already covered by the acceptance suite."""
import asyncio
import hashlib
import hmac
import json

from app import settings
from tests.conftest import drain, make_env


# --------------------------------------------------------------- §9.1 delivery

class Receiver:
    """Minimal HTTP server capturing webhook POSTs."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.requests: list[tuple[dict, bytes]] = []
        self.server = None
        self.port = None

    async def _handle(self, reader, writer):
        data = b""
        while b"\r\n\r\n" not in data:
            data += await reader.read(1024)
        head, _, rest = data.partition(b"\r\n\r\n")
        headers = {}
        for line in head.decode().split("\r\n")[1:]:
            k, _, v = line.partition(": ")
            headers[k.lower()] = v
        length = int(headers.get("content-length", 0))
        body = rest
        while len(body) < length:
            body += await reader.read(1024)
        self.requests.append((headers, body))
        writer.write(f"HTTP/1.1 {self.status_code} X\r\nContent-Length: 2\r\n\r\nok".encode())
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/webhook"


async def test_webhook_delivery_with_signature(client):
    env = await make_env(client)
    async with Receiver() as rx:
        await env.mock("POST", "/configs/webhook", {"url": rx.url, "secret": "s3cret"})
        c = await env.create_consumer()
        await env.inbound(c["id"], "signed hello")
        await drain()
        assert rx.requests, "webhook was not delivered"
        headers, body = rx.requests[0]
        assert headers["content-type"] == "application/json"
        expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert headers["x-sandbox-signature"] == f"sha256={expected}"
        payload = json.loads(body)
        assert payload["object"] == "whatsapp_business_account"
        assert payload["entry"][0]["changes"][0]["field"] == "messages"
    rows = await env.deliveries()
    assert rows[-1]["status"] == "delivered" and rows[-1]["response_code"] == 200


async def test_webhook_retry_on_failure(client, monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_MAX_ATTEMPTS", 3)
    env = await make_env(client)
    async with Receiver(status_code=500) as rx:
        await env.mock("POST", "/configs/webhook", {"url": rx.url})
        c = await env.create_consumer()
        await env.inbound(c["id"])
        await drain()
        assert len(rx.requests) == 3  # retried with backoff
    rows = await env.deliveries()
    assert rows[-1]["status"] == "failed"
    assert rows[-1]["attempts"] == 3 and rows[-1]["response_code"] == 500


async def test_deliveries_logged_without_endpoint(client):
    env = await make_env(client)
    c = await env.create_consumer()
    await env.inbound(c["id"])
    rows = await env.deliveries()
    assert rows and rows[-1]["status"] == "logged" and rows[-1]["endpoint"] is None


# ------------------------------------------------------------ §7.2 parent BSUID

async def test_parent_bsuid_lifecycle(client):
    env = await make_env(client, ga_mode=True)
    acct = await env.sandbox("POST", "/sandbox/parent_bsuid_accounts",
                             {"portfolio_ids": [env.portfolio_id]})
    assert acct["enrolled_business_portfolios"] == [env.portfolio_id]

    # Mock API view
    got = await env.mock("GET", "/parent-bsuid-accounts")
    assert got == {"parent_bsuid_account_id": acct["parent_bsuid_account_id"],
                   "enrolled_business_portfolios": [env.portfolio_id]}

    c = await env.create_consumer(username="parent.user")
    await env.inbound(c["id"])
    value = await env.last_value()
    contact = value["contacts"][0]
    assert contact["parent_user_id"].startswith("BR.ENT.")
    parent_id = contact["parent_user_id"]
    assert parent_id != contact["user_id"]

    # send targeting the parent BSUID from a number in an enrolled portfolio
    resp = await env.mock("POST", "/messages", {"recipient": parent_id, "type": "text",
                                                "text": {"body": "to parent"}})
    assert resp["contacts"][0]["input"] == parent_id
    await drain()
    last = [v for v in await env.values() if v.get("statuses")][-1]
    assert last["statuses"][0]["recipient_parent_user_id"] == parent_id

    # a non-enrolled portfolio cannot target parent BSUIDs
    pf2 = await env.sandbox("POST", "/sandbox/portfolios", {"name": "p2"})
    bn2 = await env.sandbox("POST", "/sandbox/numbers",
                            {"portfolio_id": pf2["id"], "behavior": {"service_window": "open"}})
    r = await client.post("/messages", headers={"D360-API-KEY": bn2["d360_api_key"]},
                          json={"recipient": parent_id, "type": "text", "text": {"body": "x"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == 131009

    # username changes never touch the BSUID (§7.1)
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    before = state["bsuids"]
    await env.sandbox("POST", f"/sandbox/consumers/{c['id']}/username",
                      {"username": "parent.renamed"})
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    assert state["bsuids"] == before


# ------------------------------------------------------- §8.2 marketing messages

async def test_marketing_messages(client):
    env = await make_env(client, behavior={"service_window": "open"})
    await env.mock("POST", "/v1/configs/templates",
                   {"name": "promo", "language": "en", "category": "marketing",
                    "components": []})
    await env.mock("POST", "/v1/configs/templates",
                   {"name": "receipt", "language": "en", "category": "utility",
                    "components": []})
    c = await env.create_consumer()

    resp = await env.mock("POST", "/marketing_messages",
                          {"to": c["phone"], "type": "template",
                           "template": {"name": "promo", "language": {"code": "en"}}})
    assert resp["messages"][0]["message_status"] == "accepted"
    assert resp["messages"][0]["id"].startswith("wamid.")

    r = await client.post("/marketing_messages", headers=env.mk,
                          json={"to": c["phone"], "type": "template",
                                "template": {"name": "receipt", "language": {"code": "en"}}})
    assert r.status_code == 400 and "marketing" in r.json()["error"]["error_data"]["details"]

    r = await client.post("/marketing_messages", headers=env.mk,
                          json={"to": c["phone"], "type": "text", "text": {"body": "x"}})
    assert r.status_code == 400


# ----------------------------------------------------- §3.1 behavior resolution

async def test_behavior_config_resolution(client):
    # key-level ga_mode=True overrides tenant ga_mode=False
    env = await make_env(client, ga_mode=False,
                         behavior={"ga_mode": True, "service_window": "open"})
    c = await env.create_consumer(username="cfg.user")
    await env.inbound(c["id"])
    value = await env.last_value()
    assert "wa_id" not in value["contacts"][0]  # GA behavior despite tenant pre-GA

    # GET returns the active (merged) config on both surfaces
    cfg = await env.mock("GET", "/configs/behavior")
    assert cfg["ga_mode"] is True and cfg["phone_visibility"] == "auto"
    cfg2 = await env.sandbox("GET", f"/sandbox/numbers/{env.number_id}/behavior")
    assert cfg2 == cfg

    # all-"auto" profile behaves like production: tenant pre-GA → phone visible
    await env.sandbox("PUT", f"/sandbox/numbers/{env.number_id}/behavior", {"ga_mode": None})
    await env.inbound(c["id"])
    value = await env.last_value()
    assert value["contacts"][0]["wa_id"] == c["phone"]

    # unknown keys rejected
    r = await client.put("/configs/behavior", headers=env.mk, json={"bogus_key": 1})
    assert r.status_code == 400

    # explicit username_value applied to auto-created consumers
    await env.sandbox("PUT", f"/sandbox/numbers/{env.number_id}/behavior",
                      {"end_user_has_username": True, "username_value": "fixed.handle",
                       "ga_mode": True})
    await env.mock("POST", "/messages", {"to": "5511944440000", "type": "text",
                                         "text": {"body": "auto"}})
    await drain()
    vals = [v for v in await env.values()
            if v.get("statuses") and v["statuses"][0].get("recipient_id") == "5511944440000"]
    delivered = [v for v in vals if v["statuses"][0]["status"] == "delivered"]
    assert delivered[-1]["contacts"][0]["profile"]["username"] == "fixed.handle"


async def test_tenant_default_status_delays(client):
    env = await make_env(client)
    await env.sandbox("PATCH", "/sandbox/tenants/me", {"status_delays_ms": [0, 0]})
    cfg = await env.mock("GET", "/configs/behavior")
    assert cfg["status_delays_ms"] == [0, 0]
    # key-level value beats tenant default
    await env.mock("PUT", "/configs/behavior", {"status_delays_ms": [0, 0, 0]})
    cfg = await env.mock("GET", "/configs/behavior")
    assert cfg["status_delays_ms"] == [0, 0, 0]


# ------------------------------------------------------------ §8.3 / §9.5 extras

async def test_username_endpoints_and_webhook(client):
    env = await make_env(client, ga_mode=False)
    sug = await env.mock("GET", "/username_suggestions")
    names = sug["data"][0]["username_suggestions"]
    assert len(names) == 3

    assert await env.mock("GET", "/username") == {}
    await env.mock("POST", "/username", {"username": "biz.handle"})
    assert await env.mock("GET", "/username") == {"username": "biz.handle",
                                                  "status": "reserved"}
    # GA flips status to approved on claim / active on read
    await env.sandbox("PATCH", "/sandbox/tenants/me", {"ga_mode": True})
    resp = await env.mock("POST", "/username", {"username": "biz.handle2"})
    assert resp == {"status": "approved"}
    assert (await env.mock("GET", "/username"))["status"] == "active"

    values = await env.values(field="business_username_update")
    assert values[-1] == {"display_phone_number": values[-1]["display_phone_number"],
                          "status": "approved", "username": "biz.handle2"}

    assert await env.mock("DELETE", "/username") == {"success": True}
    values = await env.values(field="business_username_update")
    assert values[-1]["status"] == "deleted" and "username" not in values[-1]
    assert await env.mock("DELETE", "/username") == {"success": False}


async def test_manual_contact_share_has_vcard(client):
    env = await make_env(client, ga_mode=True)
    c = await env.create_consumer(username="vcard.user")
    await env.sandbox("POST", f"/sandbox/consumers/{c['id']}/share_contact_manually",
                      {"to_business_number_id": env.number_id})
    value = await env.last_value()
    shared = value["messages"][0]["contacts"][0]
    assert shared["origin"] == "other"
    assert "BEGIN:VCARD" in shared["vcard"]
    assert shared["phones"][0]["phone"].startswith("+")
    assert " " not in shared["phones"][0]["wa_id"]


async def test_force_failed_wamid(client):
    env = await make_env(client, behavior={"status_sequence": ["sent"],
                                           "service_window": "open"})
    c = await env.create_consumer()
    resp = await env.mock("POST", "/messages", {"to": c["phone"], "type": "text",
                                                "text": {"body": "x"}})
    wamid = resp["messages"][0]["id"]
    await drain()
    await env.sandbox("POST", f"/sandbox/messages/{wamid}/force_failed", {"code": 131053})
    failed = [v for v in await env.values()
              for s in v.get("statuses", []) if s["status"] == "failed"]
    assert failed and failed[-1]["statuses"][0]["errors"][0]["code"] == 131053


# ------------------------------------------------------------------- misc rules

async def test_auth_and_unknown_type(client):
    env = await make_env(client, behavior={"service_window": "open"})
    r = await client.post("/messages", json={"to": "1", "type": "text",
                                             "text": {"body": "x"}})
    assert r.status_code == 401
    r = await client.post("/messages", headers={"D360-API-KEY": "nope"},
                          json={"to": "1", "type": "text", "text": {"body": "x"}})
    assert r.status_code == 401
    c = await env.create_consumer()
    r = await client.post("/messages", headers=env.mk,
                          json={"to": c["phone"], "type": "image",
                                "image": {"link": "http://x"}})
    assert r.status_code == 400
    assert "not implemented in the sandbox" in r.json()["error"]["error_data"]["details"]
    r = await client.post("/messages", headers=env.mk, json={"type": "text",
                                                             "text": {"body": "x"}})
    assert r.status_code == 400  # neither to nor recipient


async def test_malformed_bsuids(client):
    env = await make_env(client, behavior={"service_window": "open"})
    for bad in ["BR13491208655302741918", "B.13491208655302741918",
                "br.1349120865530274191", "BR.12345", "BR." + "1" * 21]:
        r = await client.post("/messages", headers=env.mk,
                              json={"recipient": bad, "type": "text", "text": {"body": "x"}})
        assert r.status_code == 400 and r.json()["error"]["code"] == 131009, bad


async def test_consumer_username_rules_via_simulation_api(client):
    env = await make_env(client)
    c = await env.create_consumer()
    r = await client.post(f"/sandbox/consumers/{c['id']}/username", headers=env.sb,
                          json={"username": "..bad"})
    assert r.status_code == 400
    await env.sandbox("POST", f"/sandbox/consumers/{c['id']}/username",
                      {"username": "valid.handle"})
    # global uniqueness across business + consumer usernames
    r = await client.post("/username", headers=env.mk, json={"username": "valid.handle"})
    assert r.status_code == 400 and r.json()["error"]["code"] == 147001
    await env.sandbox("DELETE", f"/sandbox/consumers/{c['id']}/username")
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    assert state["consumer"]["username"] is None


async def test_seed_is_deterministic():
    from app.seed import seed
    first = await seed()
    assert first["status"] == "seeded"
    assert len(first["business_numbers"]) == 2 and len(first["consumers"]) == 3
    again = await seed()
    assert again["status"] == "already seeded"
    assert again["sandbox_api_key"] == first["sandbox_api_key"]
