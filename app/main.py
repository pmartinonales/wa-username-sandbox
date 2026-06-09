from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import Base, engine
from app.errors import ApiError
from app.routers import mock, sandbox


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="username-sandbox",
    description="Stateful mock of the 360dialog WhatsApp API simulating the "
                "usernames/BSUID lifecycle. Mock API = partner-facing "
                "(D360-API-KEY); Simulation API = tester control plane "
                "(SANDBOX-API-KEY).",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.http_status, content=exc.body)


app.include_router(sandbox.router)
app.include_router(mock.router)


@app.get("/health", tags=["Ops"])
async def health():
    return {"status": "ok"}
