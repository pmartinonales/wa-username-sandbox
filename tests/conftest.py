import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="username-sandbox-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["DEFAULT_STATUS_DELAYS_MS"] = "0,0,0"
os.environ["WEBHOOK_MAX_ATTEMPTS"] = "1"
os.environ["WEBHOOK_BACKOFF_BASE_S"] = "0"
os.environ["ID_SEED"] = "test-seed"

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
    """Wait for all scheduled webhook tasks (statuses, deliveries) to finish."""
    await webhooks.wait_for_pending()


class Env:
    """One tenant + portfolio + business number, with all keys handy."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.sandbox_key: str = ""
        self.tenant_id: str = ""
        self.portfolio_id: str = ""
        self.number_id: str = ""
        self.api_key: str = ""

    @property
    def sb(self) -> dict:
        return {"SANDBOX-API-KEY": self.sandbox_key}

    @property
    def mk(self) -> dict:
        return {"D360-API-KEY": self.api_key}

    async def sandbox(self, method: str, path: str, json: dict | None = None,
                      expect: int = 200, **kw):
        r = await self.client.request(method, path, json=json, headers=self.sb, **kw)
        assert r.status_code == expect, f"{method} {path}: {r.status_code} {r.text}"
        return r.json()

    async def mock(self, method: str, path: str, json: dict | None = None,
                   expect: int = 200, key: str | None = None, **kw):
        headers = {"D360-API-KEY": key or self.api_key}
        r = await self.client.request(method, path, json=json, headers=headers, **kw)
        assert r.status_code == expect, f"{method} {path}: {r.status_code} {r.text}"
        return r.json()

    async def deliveries(self, field: str | None = None, number_id: str | None = None):
        await drain()
        params = {"page_size": 200}
        if field:
            params["field"] = field
        if number_id:
            params["business_number_id"] = number_id
        r = await self.client.get("/sandbox/webhook_deliveries", params=params, headers=self.sb)
        return r.json()["data"]

    async def last_value(self, field: str = "messages", number_id: str | None = None) -> dict:
        rows = await self.deliveries(field=field, number_id=number_id)
        assert rows, f"no '{field}' webhook deliveries"
        return rows[-1]["payload"]["entry"][0]["changes"][0]["value"]

    async def values(self, field: str = "messages", number_id: str | None = None) -> list[dict]:
        rows = await self.deliveries(field=field, number_id=number_id)
        return [r["payload"]["entry"][0]["changes"][0]["value"] for r in rows]

    async def advance(self, hours: float):
        await self.sandbox("POST", "/sandbox/time/advance", {"hours": hours})

    async def create_consumer(self, phone="5511988880001", name="Test Consumer",
                              country="BR", username=None) -> dict:
        body = {"phone": phone, "display_name": name, "country": country}
        if username:
            body["username"] = username
        return await self.sandbox("POST", "/sandbox/consumers", body)

    async def inbound(self, consumer_id: str, text="hi", number_id: str | None = None):
        return await self.sandbox("POST", f"/sandbox/consumers/{consumer_id}/send_message",
                                  {"to_business_number_id": number_id or self.number_id,
                                   "text": text})


async def make_env(client: httpx.AsyncClient, *, ga_mode: bool = False,
                   behavior: dict | None = None) -> Env:
    env = Env(client)
    r = await client.post("/sandbox/tenants", json={"name": "t", "ga_mode": ga_mode})
    assert r.status_code == 200, r.text
    data = r.json()
    env.tenant_id, env.sandbox_key = data["id"], data["sandbox_api_key"]
    pf = await env.sandbox("POST", "/sandbox/portfolios", {"name": "pf"})
    env.portfolio_id = pf["id"]
    bn = await env.sandbox("POST", "/sandbox/numbers",
                           {"portfolio_id": env.portfolio_id, "behavior": behavior or {}})
    env.number_id, env.api_key = bn["id"], bn["d360_api_key"]
    return env


def statuses_of(values: list[dict]) -> list[dict]:
    out = []
    for v in values:
        for s in v.get("statuses", []):
            out.append((v, s))
    return out
