"""§9.4 identifier-inclusion matrix, encoded as table-driven tests."""
import pytest

from tests.conftest import drain, make_env

# (has_username, phone_visible) → expectations for inbound webhooks
INBOUND_CASES = [
    # username adopted, phone visible: everything present
    dict(username="matrix.a", visibility="always",
         expect_phone=True, expect_username=True),
    # username adopted, phone hidden: BSUID-only + username
    dict(username="matrix.b", visibility="never",
         expect_phone=False, expect_username=True),
    # no username: phone always present (real rules), username never
    dict(username=None, visibility="auto",
         expect_phone=True, expect_username=False),
]


@pytest.mark.parametrize("case", INBOUND_CASES, ids=lambda c: f"user={c['username']},vis={c['visibility']}")
async def test_inbound_matrix(client, case):
    env = await make_env(client, ga_mode=True,
                         behavior={"phone_visibility": case["visibility"]})
    c = await env.create_consumer(username=case["username"])
    await env.inbound(c["id"])
    value = await env.last_value()
    contact, msg = value["contacts"][0], value["messages"][0]

    # user_id / from_user_id: always
    assert contact["user_id"] and msg["from_user_id"] == contact["user_id"]
    # wa_id / from: only if phone_visible()
    assert ("wa_id" in contact) is case["expect_phone"]
    assert ("from" in msg) is case["expect_phone"]
    # username: iff adopted, no '@' prefix
    if case["expect_username"]:
        assert contact["profile"]["username"] == case["username"]
        assert not contact["profile"]["username"].startswith("@")
    else:
        assert "username" not in contact["profile"]


STATUS_CASES = [
    # addressed by phone: wa_id/recipient_id always (even when hidden-by-rules)
    dict(addressed="phone", visibility="never", expect_phone=True),
    dict(addressed="phone", visibility="always", expect_phone=True),
    # addressed by BSUID: only if phone_visible()
    dict(addressed="bsuid", visibility="never", expect_phone=False),
    dict(addressed="bsuid", visibility="always", expect_phone=True),
]


@pytest.mark.parametrize("case", STATUS_CASES,
                         ids=lambda c: f"{c['addressed']},vis={c['visibility']}")
async def test_status_matrix(client, case):
    env = await make_env(client, ga_mode=True,
                         behavior={"phone_visibility": case["visibility"],
                                   "service_window": "open"})
    c = await env.create_consumer(username="status.matrix")
    await env.inbound(c["id"])
    state = await env.sandbox("GET", f"/sandbox/state/consumers/{c['id']}")
    bsuid = state["bsuids"][0]["bsuid"]

    if case["addressed"] == "phone":
        body = {"to": c["phone"], "type": "text", "text": {"body": "x"}}
    else:
        body = {"recipient": bsuid, "type": "text", "text": {"body": "x"}}
    resp = await env.mock("POST", "/messages", body)
    wamid = resp["messages"][0]["id"]
    await drain()

    rows = [(v, s) for v in await env.values()
            for s in v.get("statuses", []) if s["id"] == wamid]
    assert [s["status"] for _, s in rows] == ["sent", "delivered", "read"]
    for v, s in rows:
        # user_id / recipient_user_id: always
        assert s["recipient_user_id"] == bsuid
        assert v["contacts"][0]["user_id"] == bsuid
        # wa_id / recipient_id per matrix
        assert ("recipient_id" in s) is case["expect_phone"]
        assert ("wa_id" in v["contacts"][0]) is case["expect_phone"]
        # username on delivered/read only (adopted), never on sent
        has_username = "username" in v["contacts"][0]["profile"]
        assert has_username is (s["status"] in ("delivered", "read"))
