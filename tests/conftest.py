import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="username-sandbox-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["DEFAULT_STATUS_DELAYS_MS"] = "0,0,0"
os.environ["WEBHOOK_MAX_ATTEMPTS"] = "1"
os.environ["WEBHOOK_BACKOFF_BASE_S"] = "0"
os.environ["ID_SEED"] = "test-seed"
os.environ["KEYS_PER_IP_PER_HOUR"] = "10000"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import webhooks  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await webhooks.wait_for_pending()
    await engine.dispose()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def drain():
    """Wait for all scheduled webhook tasks (statuses, consumer actions)."""
    await webhooks.wait_for_pending()


# zero-delay everything: tests assert on the delivery log, not wall-clock
FAST = {"statuses": {"delays_ms": [0, 0, 0]},
        "consumer_actions": {"reply_delay_ms": 0, "tap_delay_ms": 0}}


class Env:
    """One sandbox key with helpers around the three sandbox endpoints."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.api_key: str = ""
        self.display_phone_number: str = ""
        self.waba_id: str = ""

    @property
    def hk(self) -> dict:
        return {"D360-API-KEY": self.api_key}

    async def call(self, method: str, path: str, json: dict | None = None,
                   expect: int = 200, key: str | None = None, **kw):
        headers = {"D360-API-KEY": key or self.api_key}
        r = await self.client.request(method, path, json=json, headers=headers, **kw)
        assert r.status_code == expect, f"{method} {path}: {r.status_code} {r.text}"
        return r.json()

    async def config(self, patch: dict, expect: int = 200) -> dict:
        return await self.call("PUT", "/sandbox/config", patch, expect=expect)

    async def deliveries(self, field: str | None = None) -> list[dict]:
        await drain()
        rows = (await self.call("GET", "/sandbox/webhook"))["deliveries"]
        rows.reverse()  # oldest first
        if field:
            rows = [r for r in rows if r["field"] == field]
        return rows

    async def values(self, field: str = "messages") -> list[dict]:
        rows = await self.deliveries(field=field)
        return [r["payload"]["entry"][0]["changes"][0]["value"] for r in rows]

    async def last_value(self, field: str = "messages") -> dict:
        vals = await self.values(field=field)
        assert vals, f"no '{field}' webhook deliveries"
        return vals[-1]

    async def send(self, body: dict, expect: int = 200) -> dict:
        return await self.call("POST", "/messages", body, expect=expect)


async def make_env(client: httpx.AsyncClient, *, config: dict | None = None,
                   fast: bool = True, name: str = "test") -> Env:
    env = Env(client)
    r = await client.post("/sandbox/keys", json={"name": name})
    assert r.status_code == 200, r.text
    data = r.json()
    env.api_key = data["d360_api_key"]
    env.display_phone_number = data["display_phone_number"]
    env.waba_id = data["waba_id"]
    patch = {}
    if fast:
        patch = FAST
    if config:
        from app.rules import deep_merge
        patch = deep_merge(patch, config)
    if patch:
        await env.config(patch)
    return env


def statuses_of(values: list[dict]) -> list[tuple[dict, dict]]:
    return [(v, s) for v in values for s in v.get("statuses", [])]


def inbound_of(values: list[dict]) -> list[dict]:
    return [v for v in values if "messages" in v and "statuses" not in v]
