from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.db import Base, engine
from app.errors import ApiError
from app.reqlog import RequestLogMiddleware
from app.routers import mock, sandbox

DESCRIPTION = """
Stateful mock of the 360dialog WhatsApp API simulating the **username/BSUID**
lifecycle — for partners validating their integrations before Meta enables
usernames globally.

**Five-minute path:**

1. `POST /sandbox/keys` → get a `D360-API-KEY`
2. `PUT /sandbox/webhook` → point webhooks at your endpoint
3. `PUT /sandbox/config` → choose the behavior you want to see
4. ...then call only real API endpoints (`/messages`, `/username`, ...)

Only the three `/sandbox/*` endpoints are sandbox-specific; everything else is
exactly the API Meta is releasing. Webhook payload reference: [`/docs/webhooks`](/docs/webhooks).
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="username-sandbox",
    version="2.0.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "Sandbox", "description": "The only three endpoints that do not "
                                           "exist in the real API."},
        {"name": "Messages", "description": "Real API surface (Meta/360dialog)."},
        {"name": "Business username", "description": "Real API surface."},
        {"name": "Contact book", "description": "Real API surface."},
        {"name": "Templates", "description": "Real API surface."},
    ],
)
app.include_router(sandbox.router)
app.include_router(mock.router)

# the dashboard UI (and partner tooling) may run on any origin; no cookies/auth
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
app.add_middleware(RequestLogMiddleware)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.http_status, content=exc.body)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/docs/webhooks", include_in_schema=False)
async def webhook_reference():
    path = Path(__file__).resolve().parent.parent / "WEBHOOKS.md"
    return PlainTextResponse(path.read_text(), media_type="text/markdown; charset=utf-8")
