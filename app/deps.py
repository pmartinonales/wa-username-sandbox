from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import rules
from app.db import get_session
from app.errors import ApiError
from app.models import ApiKey


@dataclass
class Ctx:
    session: AsyncSession
    key: ApiKey


async def auth(session: AsyncSession = Depends(get_session),
               d360_api_key: str | None = Header(None, alias="D360-API-KEY")) -> Ctx:
    if not d360_api_key:
        raise ApiError(401, "Missing D360-API-KEY header. Create one with POST /sandbox/keys.",
                       http_status=401)
    key = (await session.execute(select(ApiKey).where(
        ApiKey.d360_api_key == d360_api_key))).scalar_one_or_none()
    if key is None:
        raise ApiError(401, "Unknown D360-API-KEY. Create one with POST /sandbox/keys.",
                       http_status=401)
    if key.expires_at < rules.now():
        raise ApiError(401, f"This sandbox key expired at {key.expires_at.isoformat()}Z. "
                            "Create a new one with POST /sandbox/keys.", http_status=401)
    return Ctx(session=session, key=key)
