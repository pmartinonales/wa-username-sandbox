from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings
from app.db import get_session
from app.errors import ApiError
from app.models import BusinessNumber, Portfolio, Tenant


@dataclass
class MockContext:
    session: AsyncSession
    bn: BusinessNumber
    portfolio: Portfolio
    tenant: Tenant


async def mock_auth(session: AsyncSession = Depends(get_session),
                    d360_api_key: str | None = Header(None, alias="D360-API-KEY")) -> MockContext:
    if not d360_api_key:
        raise ApiError(401, "Missing D360-API-KEY header.", http_status=401)
    bn = (await session.execute(select(BusinessNumber).where(
        BusinessNumber.d360_api_key == d360_api_key))).scalar_one_or_none()
    if bn is None:
        raise ApiError(401, "Unknown D360-API-KEY.", http_status=401)
    portfolio = await session.get(Portfolio, bn.portfolio_id)
    tenant = await session.get(Tenant, portfolio.tenant_id)
    return MockContext(session=session, bn=bn, portfolio=portfolio, tenant=tenant)


@dataclass
class SandboxContext:
    session: AsyncSession
    tenant: Tenant


async def sandbox_auth(session: AsyncSession = Depends(get_session),
                       sandbox_api_key: str | None = Header(None, alias="SANDBOX-API-KEY")) -> SandboxContext:
    if not sandbox_api_key:
        raise ApiError(401, "Missing SANDBOX-API-KEY header.", http_status=401)
    tenant = (await session.execute(select(Tenant).where(
        Tenant.sandbox_api_key == sandbox_api_key))).scalar_one_or_none()
    if tenant is None:
        raise ApiError(401, "Unknown SANDBOX-API-KEY.", http_status=401)
    return SandboxContext(session=session, tenant=tenant)


async def master_auth(x_master_key: str | None = Header(None, alias="X-MASTER-KEY")) -> None:
    if settings.MASTER_KEY and x_master_key != settings.MASTER_KEY:
        raise ApiError(401, "Invalid or missing X-MASTER-KEY.", http_status=401)
