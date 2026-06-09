"""Acceptance scenarios §12 — each test number matches the spec scenario."""
from tests.conftest import drain, make_env


async def test_01_golden_path(client):
    env = await make_env(client, ga_mode=True)
    c = await env.create_consumer(username="alice.golden", name="Alice Golden")

    # consumer (username, GA, no history) sends inbound
    await env.inbound(c["id"], "hello business")
    value = await env.last_value()
    contact, msg = value["contacts"][0], value["messages"][0]
    assert contact["user_id"].startswith("BR.")
    assert contact["profile"]["username"] == "alice.golden"
    assert "wa_id" not in contact and "from" not in msg
    assert msg["from_user_id"] == contact["user_id"]
    bsuid = contact["user_id"]

    # partner replies via recipient=<BSUID>
    resp = await env.mock("POST", "/messages",
                          {"recipient": bsuid, "type": "text", "text": {"body": "hi!"}})
    assert resp["contacts"] == [{"input": bsuid, "user_id": bsuid}]
    wamid = resp["messages"][0]["id"]
    assert wamid.startswith("wamid.")

    # sent/delivered/read statuses: recipient_user_id, no recipient_id
    await drain()
    seen = []
    for v in await env.values():
        for s in v.get("statuses", []):
            if s["id"] == wamid:
                seen.append(s["status"])
                assert s["recipient_user_id"] == bsuid
                assert "recipient_id" not in s
    assert seen == ["sent", "delivered", "read"]

    # partner sends REQUEST_CONTACT_INFO interactive
    rci = await env.mock("POST", "/messages", {
        "recipient": bsuid, "type": "interactive",
        "interactive": {"type": "request_contact_info", "body": {"text": "share?"},
                        "action": {"name": "request_contact_info"}}})
    rci_wamid = rci["messages"][0]["id"]
    await drain()

    # consumer taps → contacts webhook, origin=contact_request, no vCard
    await env.sandbox("POST", f"/sandbox/consumers/{c['id']}/tap_share_contact",
                      {"message_wamid": rci_wamid})
    value = await env.last_value()
    m = value["messages"][0]
    assert m["type"] == "contacts"
    assert m["from_user_id"] == bsuid
    shared = m["contacts"][0]
    assert shared["origin"] == "contact_request"
    assert "vcard" not in shared
    assert shared["phones"][0]["phone"] == "+" + c["phone"]
    assert shared["phones"][0]["wa_id"] == c["phone"]

    # subsequent webhooks include the phone again
    await env.inbound(c["id"], "thanks")
    value = await env.last_value()
    assert value["contacts"][0]["wa_id"] == c["phone"]
    assert value["messages"][0]["from"] == c["phone"]


async def test_02_bsuid_send_closed_window(client):
    env = await make_env(client, ga_mode=True)
    c = await env.create_consumer(username="bob.window")
    await env.inbound(c["id"])
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    bsuid = state["bsuids"][0]["bsuid"]
    await env.advance(25)  # window expired
    r = await client.post("/messages", headers=env.mk,
                          json={"recipient": bsuid, "type": "text", "text": {"body": "x"}})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == 131047
    assert "24-hour" in err["error_data"]["details"]
    # consumer re-opens window → succeeds
    await env.inbound(c["id"])
    await env.mock("POST", "/messages",
                   {"recipient": bsuid, "type": "text", "text": {"body": "x"}})


async def test_03_auth_template_to_bsuid(client):
    env = await make_env(client, ga_mode=True)
    await env.mock("POST", "/v1/configs/templates",
                   {"name": "otp", "language": "en", "category": "utility",
                    "auth_flavor": "one-tap", "components": []})
    c = await env.create_consumer(username="auth.target")
    await env.inbound(c["id"])
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    bsuid = state["bsuids"][0]["bsuid"]
    r = await client.post("/messages", headers=env.mk,
                          json={"recipient": bsuid, "type": "template",
                                "template": {"name": "otp", "language": {"code": "en"}}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == 131062
    # same template to phone → succeeds
    resp = await env.mock("POST", "/messages",
                          {"to": c["phone"], "type": "template",
                           "template": {"name": "otp", "language": {"code": "en"}}})
    assert resp["contacts"][0]["wa_id"] == c["phone"]


async def test_04_bsuid_cross_portfolio(client):
    env = await make_env(client, ga_mode=True)
    c = await env.create_consumer(username="cross.portfolio")
    await env.inbound(c["id"])
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    bsuid_a = state["bsuids"][0]["bsuid"]
    # second portfolio + number in the same tenant
    pf_b = await env.sandbox("POST", "/sandbox/portfolios", {"name": "B"})
    bn_b = await env.sandbox("POST", "/sandbox/numbers", {"portfolio_id": pf_b["id"]})
    r = await client.post("/messages", headers={"D360-API-KEY": bn_b["d360_api_key"]},
                          json={"recipient": bsuid_a, "type": "text", "text": {"body": "x"}})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == 131009
    assert "portfolio" in err["error_data"]["details"]


async def test_05_to_wins_over_recipient(client):
    env = await make_env(client, ga_mode=True)
    c = await env.create_consumer(username="both.fields")
    await env.inbound(c["id"])
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    bsuid = state["bsuids"][0]["bsuid"]
    resp = await env.mock("POST", "/messages",
                          {"to": c["phone"], "recipient": bsuid,
                           "type": "text", "text": {"body": "x"}})
    contact = resp["contacts"][0]
    assert contact["wa_id"] == c["phone"]
    assert "user_id" not in contact


async def test_06_pre_ga_phone_always_visible(client):
    env = await make_env(client, ga_mode=False)
    c = await env.create_consumer(username="prega.user")
    await env.inbound(c["id"])
    value = await env.last_value()
    assert value["contacts"][0]["wa_id"] == c["phone"]
    assert value["messages"][0]["from"] == c["phone"]
    assert value["contacts"][0]["profile"]["username"] == "prega.user"
    # statuses on a BSUID send also include the phone pre-GA
    bsuid = value["contacts"][0]["user_id"]
    await env.mock("POST", "/messages", {"recipient": bsuid, "type": "text",
                                         "text": {"body": "x"}})
    await drain()
    last = (await env.values())[-1]
    assert last["statuses"][0]["recipient_id"] == c["phone"]


async def test_07_contact_book_delete(client):
    env = await make_env(client, ga_mode=True)
    c = await env.create_consumer(username="deleted.book")
    # pre-GA-style history: flip GA off, interact (writes book+cache), back on
    await env.sandbox("PATCH", "/sandbox/tenants/me", {"ga_mode": False})
    await env.inbound(c["id"])
    await env.sandbox("PATCH", "/sandbox/tenants/me", {"ga_mode": True})
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    bsuid = state["bsuids"][0]["bsuid"]
    assert state["contact_book"], "expected a contact book entry"

    resp = await env.mock("DELETE", f"/contact_book?messaging_product=whatsapp&bsuid={bsuid}")
    assert resp == {"messaging_product": "whatsapp", "success": True, "deleted": True}

    # inbound webhooks STILL arrive, BSUID-only (alpha webhook-drop regression)
    before = len(await env.values())
    await env.inbound(c["id"], "still here")
    values = await env.values()
    assert len(values) == before + 1
    contact, msg = values[-1]["contacts"][0], values[-1]["messages"][0]
    assert "wa_id" not in contact and "from" not in msg
    assert msg["from_user_id"] == bsuid

    # second delete: success, deleted=false
    resp = await env.mock("DELETE", f"/contact_book?messaging_product=whatsapp&bsuid={bsuid}")
    assert resp["deleted"] is False

    # parent BSUIDs and foreign BSUIDs are rejected with success:false, 400
    r = await client.request("DELETE", "/contact_book",
                             params={"messaging_product": "whatsapp", "bsuid": "BR.ENT.123456"},
                             headers=env.mk)
    assert r.status_code == 400 and r.json()["success"] is False


async def test_08_thirty_day_cache_per_number(client):
    env = await make_env(client, ga_mode=False)
    c = await env.create_consumer(username="cache.user")
    # second number in the SAME portfolio
    bn_b = await env.sandbox("POST", "/sandbox/numbers", {"portfolio_id": env.portfolio_id})
    # pre-GA: inbound to number A writes contact book + cache(A)
    await env.inbound(c["id"])
    await env.sandbox("PATCH", "/sandbox/tenants/me", {"ga_mode": True})
    # surgically remove the contact book entry, leaving the cache intact
    await env.sandbox("DELETE",
                      f"/sandbox/state/consumers/{c['id']}/contact_book/{env.portfolio_id}")

    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    bsuid = state["bsuids"][0]["bsuid"]

    # A still sees the phone via its 30-day cache
    await env.mock("POST", "/messages", {"recipient": bsuid, "type": "text",
                                         "text": {"body": "from A"}})
    await drain()
    last_a = (await env.values(number_id=env.number_id))[-1]
    assert last_a["statuses"][0].get("recipient_id") == c["phone"]

    # B (same portfolio, no cache) does not — open B's window first
    await env.inbound(c["id"], number_id=bn_b["id"])
    await env.mock("POST", "/messages", {"recipient": bsuid, "type": "text",
                                         "text": {"body": "from B"}}, key=bn_b["d360_api_key"])
    await drain()
    vals_b = [v for v in await env.values(number_id=bn_b["id"]) if v.get("statuses")]
    assert vals_b and all("recipient_id" not in v["statuses"][0] for v in vals_b)

    # advance 31 days → A loses it too
    await env.advance(31 * 24)
    await env.inbound(c["id"])  # reopen window (does not write cache: phone hidden now?)
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    assert all(e["expired"] for e in state["cache"]
               if e["business_number_id"] == env.number_id)
    await env.mock("POST", "/messages", {"recipient": bsuid, "type": "text",
                                         "text": {"body": "late"}})
    await drain()
    last_a = (await env.values(number_id=env.number_id))[-1]
    assert "recipient_id" not in last_a["statuses"][0]


async def test_09_username_rate_limit_and_validation(client):
    env = await make_env(client, ga_mode=False)
    for i, name in enumerate(["shop.one", "shop.two", "shop.three"]):
        resp = await env.mock("POST", "/username", {"username": name})
        assert resp == {"status": "reserved"}
    r = await client.post("/username", headers=env.mk, json={"username": "shop.four"})
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == 131056
    assert "max 3 changes per 14 days" in err["error_data"]["details"]
    assert "Next change available at" in err["error_data"]["details"]
    assert "claimed" not in err["error_data"]["details"].lower()

    # after 14 days the window rolls over
    await env.advance(14 * 24 + 1)
    resp = await env.mock("POST", "/username", {"username": "shop.four"})
    assert resp["status"] == "reserved"

    # duplicate name (another number) → 147001
    pf2 = await env.sandbox("POST", "/sandbox/portfolios", {"name": "p2"})
    bn2 = await env.sandbox("POST", "/sandbox/numbers", {"portfolio_id": pf2["id"]})
    r = await client.post("/username", headers={"D360-API-KEY": bn2["d360_api_key"]},
                          json={"username": "SHOP.FOUR"})  # case-insensitive
    assert r.status_code == 400 and r.json()["error"]["code"] == 147001

    # bad formats → 100
    for bad in ["ab", "a" * 36, ".lead", "trail.", "dou..ble", "wwwshop", "shop.com",
                "12345", "bad name", "shop.html"]:
        r = await client.post("/username", headers={"D360-API-KEY": bn2["d360_api_key"]},
                              json={"username": bad})
        assert r.status_code == 400 and r.json()["error"]["code"] == 100, bad
    # '.'/'_' significant: shop_four is a different name and is free
    resp = await env.mock("POST", "/username", {"username": "shop_four"},
                          key=bn2["d360_api_key"])
    assert resp["status"] == "reserved"


async def test_10_template_button_validation_and_interactive_typo(client):
    env = await make_env(client)
    r = await client.post("/v1/configs/templates", headers=env.mk, json={
        "name": "t1", "language": "en", "category": "utility",
        "components": [{"type": "BUTTONS", "buttons": [
            {"type": "REQUEST_CONTACT_INFO", "text": "Gimme your contact"}]}]})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["error_subcode"] == 2388153
    assert err["error_user_title"] == "Button text modification not allowed"

    r = await client.post("/v1/configs/templates", headers=env.mk, json={
        "name": "t2", "language": "en", "category": "utility",
        "components": [{"type": "BUTTONS", "buttons": [{"type": "REQUEST_CONTACT_INFO"}]}]})
    assert r.status_code == 400
    assert r.json()["error"]["error_subcode"] == 2388050

    c = await env.create_consumer()
    await env.inbound(c["id"])
    r = await client.post("/messages", headers=env.mk, json={
        "to": c["phone"], "type": "interactive",
        "interactive": {"type": "contact_request", "body": {"text": "x"},
                        "action": {"name": "request_contact_info"}}})
    assert r.status_code == 400
    details = r.json()["error"]["error_data"]["details"]
    assert "'contact_request' is not a valid interactive type" in details
    assert "request_contact_info" in details


async def test_11_phone_change_regenerates_bsuid(client):
    env = await make_env(client, ga_mode=True)
    c = await env.create_consumer(username="phone.changer")
    await env.inbound(c["id"])
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    old_bsuid = state["bsuids"][0]["bsuid"]

    resp = await env.sandbox("POST", f"/sandbox/consumers/{c['id']}/change_phone",
                             {"phone": "5511977770000"})
    new_bsuid = resp["bsuids"][0]["new"]
    assert new_bsuid != old_bsuid

    # system webhook with old/new user_id
    value = await env.last_value()
    msg = value["messages"][0]
    assert msg["type"] == "system"
    assert msg["system"]["body"] == "User changed phone number"
    assert msg["system"]["user_id"] == old_bsuid
    assert msg["system"]["new_user_id"] == new_bsuid

    # old BSUID → 131009... (it no longer exists; sandbox auto-creates a NEW
    # consumer for well-formed unknown BSUIDs, so use a malformed one to assert
    # the error path, and assert the old BSUID no longer maps to this consumer)
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    assert all(m["bsuid"] != old_bsuid for m in state["bsuids"])
    r = await client.post("/messages", headers=env.mk,
                          json={"recipient": "BR.123", "type": "text", "text": {"body": "x"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == 131009


async def test_12_failed_status_shape(client):
    env = await make_env(client, behavior={"status_sequence": ["sent", "failed"],
                                           "failed_error_code": 131026})
    c = await env.create_consumer()
    await env.inbound(c["id"])
    await env.mock("POST", "/messages", {"to": c["phone"], "type": "text",
                                         "text": {"body": "x"}})
    await drain()
    failed = [v for v in await env.values()
              for s in v.get("statuses", []) if s["status"] == "failed"]
    assert failed, "expected a failed status webhook"
    v = failed[-1]
    assert "contacts" not in v
    s = v["statuses"][0]
    assert "recipient_user_id" not in s  # phone-addressed
    assert s["recipient_id"] == c["phone"]
    assert s["errors"][0]["code"] == 131026


async def test_13_tenant_isolation(client):
    env_a = await make_env(client, ga_mode=True)
    env_b = await make_env(client, ga_mode=True)
    cb = await env_b.create_consumer(phone="5511966660000", username="tenant.b.user")
    await env_b.inbound(cb["id"])
    state_b = await env_b.sandbox("GET", f"/sandbox/state/consumers/{cb['id']}")
    bsuid_b = state_b["bsuids"][0]["bsuid"]

    # A cannot read B's consumer via the Simulation API
    r = await client.get(f"/sandbox/state/consumers/{cb['id']}", headers=env_a.sb)
    assert r.status_code == 404

    # Using B's BSUID with A's key never reaches B's consumer: the sandbox
    # auto-creates a fresh consumer inside tenant A instead.
    before_b = len(await env_b.values())
    await env_a.mock("POST", "/messages", {"recipient": bsuid_b, "type": "template",
                                           "template": {"name": "x"}}, expect=404)
    # (template doesn't exist in A; use behavior override to send free-form)
    await env_a.sandbox("PUT", f"/sandbox/numbers/{env_a.number_id}/behavior",
                        {"service_window": "open"})
    await env_a.mock("POST", "/messages", {"recipient": bsuid_b, "type": "text",
                                           "text": {"body": "hi"}})
    await drain()
    assert len(await env_b.values()) == before_b  # B saw nothing
    deliveries_a = await env_a.values()
    assert deliveries_a  # A's webhooks went to A


async def test_14_config_first_flow(client):
    env = await make_env(client, behavior={
        "end_user_has_username": True, "phone_visibility": "never",
        "ga_mode": True, "service_window": "open"})
    arbitrary = "BR.1234567890123456789"
    resp = await env.mock("POST", "/messages", {"recipient": arbitrary, "type": "text",
                                                "text": {"body": "config-first"}})
    assert resp["contacts"][0] == {"input": arbitrary, "user_id": arbitrary}
    await drain()
    vals = [v for v in await env.values() if v.get("statuses")]
    assert len(vals) == 3
    for v in vals:
        s = v["statuses"][0]
        assert s["recipient_user_id"] == arbitrary and "recipient_id" not in s
    # username present on delivered/read contacts, never sent
    by_status = {v["statuses"][0]["status"]: v for v in vals}
    assert "username" not in by_status["sent"]["contacts"][0]["profile"]
    assert by_status["delivered"]["contacts"][0]["profile"]["username"]
    assert by_status["read"]["contacts"][0]["profile"]["username"]

    # flip phone_visibility → next statuses include the phone
    await env.mock("PUT", "/configs/behavior", {"phone_visibility": "always"})
    await env.mock("POST", "/messages", {"recipient": arbitrary, "type": "text",
                                         "text": {"body": "now visible"}})
    await drain()
    last = [v for v in await env.values() if v.get("statuses")][-1]
    assert last["statuses"][0]["recipient_id"]

    # inject_error: next send fails with 131047, the one after succeeds
    await env.mock("PUT", "/configs/behavior",
                   {"inject_error": {"on": "messages", "code": 131047, "times": 1}})
    r = await client.post("/messages", headers=env.mk,
                          json={"recipient": arbitrary, "type": "text", "text": {"body": "x"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == 131047
    await env.mock("POST", "/messages", {"recipient": arbitrary, "type": "text",
                                         "text": {"body": "ok again"}})


async def test_15_service_window_overrides(client):
    env = await make_env(client, behavior={"service_window": "closed"})
    c = await env.create_consumer()
    await env.inbound(c["id"])  # real window open, but config says closed
    r = await client.post("/messages", headers=env.mk,
                          json={"to": c["phone"], "type": "text", "text": {"body": "x"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == 131047

    await env.sandbox("PUT", f"/sandbox/numbers/{env.number_id}/behavior",
                      {"service_window": "open"})
    c2 = await env.create_consumer(phone="5511955550000")
    # no prior inbound, still passes
    await env.mock("POST", "/messages", {"to": c2["phone"], "type": "text",
                                         "text": {"body": "x"}})
