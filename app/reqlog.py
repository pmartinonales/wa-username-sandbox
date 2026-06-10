"""Request/response logging middleware for the monitoring UI.

Records every API call (method, path, status, bodies, error code, duration)
into RequestLog. Monitoring/infra paths are skipped so UI polling doesn't
spam its own log."""
import json
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import ids, rules
from app.db import SessionLocal
from app.models import RequestLog

SKIP_PATHS = {"/health", "/openapi.json", "/docs", "/docs/webhooks",
              "/sandbox/requests", "/favicon.ico"}
SKIP_GET = {"/sandbox/webhook"}  # polled by the UI; PUT is still logged
MAX_BODY = 10_000


def _truncate(raw: bytes) -> str | None:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    return text[:MAX_BODY] + ("…" if len(text) > MAX_BODY else "")


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in SKIP_PATHS or path.startswith("/docs/") or \
                (request.method == "GET" and path in SKIP_GET) or \
                request.method == "OPTIONS":
            return await call_next(request)

        req_body = await request.body()
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        resp_body = b""
        async for chunk in response.body_iterator:
            resp_body += chunk
        rebuilt = Response(content=resp_body, status_code=response.status_code,
                           headers=dict(response.headers),
                           media_type=response.media_type)

        api_key = request.headers.get("D360-API-KEY")
        error_code = None
        try:
            parsed = json.loads(resp_body) if resp_body else {}
            if isinstance(parsed, dict):
                if "error" in parsed:
                    error_code = parsed["error"].get("code")
                # key creation has no header — attribute the log to the new key
                if api_key is None and path == "/sandbox/keys":
                    api_key = parsed.get("d360_api_key")
        except ValueError:
            pass

        try:
            async with SessionLocal() as session:
                session.add(RequestLog(
                    id=await ids.new_id(session, "rq"), api_key=api_key,
                    method=request.method, path=path,
                    status_code=response.status_code, error_code=error_code,
                    request_body=_truncate(req_body),
                    response_body=_truncate(resp_body),
                    duration_ms=duration_ms, created_at=rules.now()))
                await session.commit()
        except Exception:  # logging must never break the API itself
            pass
        return rebuilt
