"""The rules engine: behavior-config resolution, phone visibility (§5),
contact book & cache mechanics (§6), service windows, BSUID resolution (§7),
username validation (§8.3)."""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import ids, settings
from app.errors import ApiError
from app.models import (
    BsuidMapping,
    BusinessNumber,
    CacheEntry,
    Consumer,
    ContactBookEntry,
    ParentAccount,
    ParentBsuid,
    ParentEnrollment,
    Portfolio,
    ServiceWindow,
    Tenant,
)

BSUID_RE = re.compile(r"^[A-Z]{2}\.\d{18,20}$")
PARENT_BSUID_RE = re.compile(r"^[A-Z]{2}\.ENT\.\d+$")

BEHAVIOR_DEFAULTS = {
    "end_user_has_username": False,
    "username_value": "auto",
    "phone_visibility": "auto",
    "parent_bsuid": False,
    "service_window": "auto",
    "consumer_country": "BR",
    "ga_mode": None,
    "status_sequence": ["sent", "delivered", "read"],
    "status_delays_ms": None,  # falls back to tenant default, then settings default
    "failed_error_code": 131049,
    "inject_error": None,
}

_ENUMS = {
    "phone_visibility": {"auto", "always", "never"},
    "service_window": {"auto", "open", "closed"},
}
_STATUSES = {"sent", "delivered", "read", "failed"}


def validate_behavior_patch(patch: dict) -> dict:
    unknown = set(patch) - set(BEHAVIOR_DEFAULTS)
    if unknown:
        raise ApiError(100, f"Unknown behavior config keys: {sorted(unknown)}")
    for key, allowed in _ENUMS.items():
        if key in patch and patch[key] not in allowed:
            raise ApiError(100, f"behavior.{key} must be one of {sorted(allowed)}")
    if "status_sequence" in patch:
        seq = patch["status_sequence"]
        if not isinstance(seq, list) or not set(seq) <= _STATUSES:
            raise ApiError(100, f"behavior.status_sequence entries must be in {sorted(_STATUSES)}")
    ie = patch.get("inject_error")
    if ie is not None and not (isinstance(ie, dict) and "on" in ie and "code" in ie):
        raise ApiError(100, 'behavior.inject_error must be null or {"on": ..., "code": ..., "times": N}')
    return patch


def effective_config(bn: BusinessNumber, tenant: Tenant) -> dict:
    cfg = dict(BEHAVIOR_DEFAULTS)
    if bn.behavior:
        cfg.update(bn.behavior.config or {})
    if cfg["status_delays_ms"] is None:
        cfg["status_delays_ms"] = tenant.status_delays_ms or settings.DEFAULT_STATUS_DELAYS_MS
    return cfg


def effective_ga_mode(cfg: dict, tenant: Tenant) -> bool:
    return tenant.ga_mode if cfg.get("ga_mode") is None else bool(cfg["ga_mode"])


def now_for(tenant: Tenant) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=tenant.clock_offset_s)


async def pop_injected_error(session: AsyncSession, bn: BusinessNumber, endpoint: str) -> None:
    """§3.1 inject_error: fail the next N matching Mock API calls. The decrement
    is committed before raising so the injected error fires exactly N times."""
    if not bn.behavior:
        return
    ie = (bn.behavior.config or {}).get("inject_error")
    if not ie or ie.get("on") != endpoint or ie.get("times", 1) <= 0:
        return
    ie["times"] = ie.get("times", 1) - 1
    if ie["times"] <= 0:
        bn.behavior.config["inject_error"] = None
    flag_modified(bn.behavior, "config")
    await session.commit()
    raise ApiError(int(ie["code"]), f"Sandbox-injected error on '{endpoint}' (behavior config inject_error).")


# ---------------------------------------------------------------- visibility

async def phone_visible(session: AsyncSession, bn: BusinessNumber, consumer: Consumer,
                        portfolio: Portfolio, tenant: Tenant, cfg: dict | None = None) -> bool:
    cfg = cfg or effective_config(bn, tenant)
    if cfg["phone_visibility"] == "always":
        return True
    if cfg["phone_visibility"] == "never":
        return False
    if not effective_ga_mode(cfg, tenant):
        return True  # pre-GA: phones always visible
    if consumer.username is None:
        return True  # user never adopted a username
    entry = (await session.execute(select(ContactBookEntry).where(
        ContactBookEntry.portfolio_id == portfolio.id,
        ContactBookEntry.consumer_id == consumer.id,
        ContactBookEntry.phone_known.is_(True)))).scalar_one_or_none()
    if entry:
        return True
    cutoff = now_for(tenant) - timedelta(days=30)
    cache = (await session.execute(select(CacheEntry).where(
        CacheEntry.business_number_id == bn.id,  # 30-day cache is PER BUSINESS NUMBER
        CacheEntry.consumer_id == consumer.id,
        CacheEntry.phone_known.is_(True),
        CacheEntry.last_interaction_at >= cutoff))).scalar_one_or_none()
    return cache is not None


# ------------------------------------------------------- contact book & cache

async def touch_contact(session: AsyncSession, portfolio: Portfolio, bn: BusinessNumber,
                        consumer: Consumer, when: datetime, phone_known: bool = True) -> None:
    entry = (await session.execute(select(ContactBookEntry).where(
        ContactBookEntry.portfolio_id == portfolio.id,
        ContactBookEntry.consumer_id == consumer.id))).scalar_one_or_none()
    if entry is None:
        session.add(ContactBookEntry(id=await ids.new_id(session, "cb"), portfolio_id=portfolio.id,
                                     consumer_id=consumer.id, created_at=when, phone_known=phone_known))
    elif phone_known and not entry.phone_known:
        entry.phone_known = True
    cache = (await session.execute(select(CacheEntry).where(
        CacheEntry.business_number_id == bn.id,
        CacheEntry.consumer_id == consumer.id))).scalar_one_or_none()
    if cache is None:
        session.add(CacheEntry(id=await ids.new_id(session, "ce"), business_number_id=bn.id,
                               consumer_id=consumer.id, last_interaction_at=when, phone_known=phone_known))
    else:
        if phone_known:
            cache.phone_known = True
        elif cache.last_interaction_at < when - timedelta(days=30):
            # an expired entry refreshed by a BSUID-only interaction must not
            # resurrect phone visibility — the cache already forgot the phone
            cache.phone_known = False
        cache.last_interaction_at = when


# ------------------------------------------------------------ service window

async def window_open(session: AsyncSession, bn: BusinessNumber, consumer: Consumer,
                      tenant: Tenant, cfg: dict) -> bool:
    if cfg["service_window"] == "open":
        return True
    if cfg["service_window"] == "closed":
        return False
    win = (await session.execute(select(ServiceWindow).where(
        ServiceWindow.business_number_id == bn.id,
        ServiceWindow.consumer_id == consumer.id))).scalar_one_or_none()
    return win is not None and win.opened_at > now_for(tenant) - timedelta(hours=24)


async def open_window(session: AsyncSession, bn: BusinessNumber, consumer: Consumer,
                      when: datetime) -> None:
    win = (await session.execute(select(ServiceWindow).where(
        ServiceWindow.business_number_id == bn.id,
        ServiceWindow.consumer_id == consumer.id))).scalar_one_or_none()
    if win is None:
        session.add(ServiceWindow(id=await ids.new_id(session, "sw"), business_number_id=bn.id,
                                  consumer_id=consumer.id, opened_at=when))
    else:
        win.opened_at = when


# ----------------------------------------------------------- BSUID & parents

async def get_or_create_bsuid(session: AsyncSession, consumer: Consumer, portfolio: Portfolio,
                              supplied: str | None = None) -> str:
    mapping = (await session.execute(select(BsuidMapping).where(
        BsuidMapping.consumer_id == consumer.id,
        BsuidMapping.portfolio_id == portfolio.id))).scalar_one_or_none()
    if mapping:
        return mapping.bsuid
    value = supplied or ids.make_bsuid(consumer.country, consumer.id, portfolio.id)
    session.add(BsuidMapping(id=await ids.new_id(session, "bm"), consumer_id=consumer.id,
                             portfolio_id=portfolio.id, bsuid=value))
    return value


async def portfolio_parent_account(session: AsyncSession, portfolio: Portfolio) -> ParentAccount | None:
    enr = (await session.execute(select(ParentEnrollment).where(
        ParentEnrollment.portfolio_id == portfolio.id))).scalar_one_or_none()
    return await session.get(ParentAccount, enr.parent_account_id) if enr else None


async def get_or_create_parent_bsuid(session: AsyncSession, consumer: Consumer,
                                     account: ParentAccount, supplied: str | None = None) -> str:
    row = (await session.execute(select(ParentBsuid).where(
        ParentBsuid.consumer_id == consumer.id,
        ParentBsuid.parent_account_id == account.id))).scalar_one_or_none()
    if row:
        return row.value
    value = supplied or ids.make_parent_bsuid(consumer.country, consumer.id, account.id)
    session.add(ParentBsuid(id=await ids.new_id(session, "pb"), consumer_id=consumer.id,
                            parent_account_id=account.id, value=value))
    return value


async def parent_id_for(session: AsyncSession, consumer: Consumer, portfolio: Portfolio,
                        cfg: dict, tenant: Tenant) -> str | None:
    """parent_user_id is included when the portfolio is enrolled, or forced via config."""
    account = await portfolio_parent_account(session, portfolio)
    if account is None and cfg.get("parent_bsuid"):
        # config-first: auto-create + enroll a parent account so the fields appear
        account = ParentAccount(id=await ids.new_id(session, "pa"), tenant_id=tenant.id)
        session.add(account)
        await session.flush()
        session.add(ParentEnrollment(id=await ids.new_id(session, "pe"),
                                     parent_account_id=account.id, portfolio_id=portfolio.id))
        portfolio.parent_bsuid_enabled = True
    if account is None:
        return None
    return await get_or_create_parent_bsuid(session, consumer, account)


# --------------------------------------------------------- auto-consumer (§3.1)

async def auto_create_consumer(session: AsyncSession, tenant: Tenant, cfg: dict, *,
                               phone: str | None = None, country: str | None = None) -> Consumer:
    cid = await ids.new_id(session, "cs")
    username = None
    if cfg["end_user_has_username"]:
        username = cfg["username_value"] if cfg["username_value"] != "auto" else f"user.{ids.digits(8, 'uname', cid)}"
    consumer = Consumer(id=cid, tenant_id=tenant.id, phone=phone or ids.make_phone(cid),
                        display_name=f"Sandbox Consumer {cid[-6:]}",
                        username=username, country=(country or cfg["consumer_country"]).upper())
    session.add(consumer)
    await session.flush()
    return consumer


async def resolve_phone_recipient(session: AsyncSession, tenant: Tenant, cfg: dict, to: str) -> Consumer:
    phone = re.sub(r"[^\d]", "", to)
    if not phone:
        raise ApiError(131009, f"Invalid phone number: {to!r}")
    consumer = (await session.execute(select(Consumer).where(
        Consumer.tenant_id == tenant.id, Consumer.phone == phone))).scalar_one_or_none()
    return consumer or await auto_create_consumer(session, tenant, cfg, phone=phone)


async def resolve_bsuid_recipient(session: AsyncSession, tenant: Tenant, portfolio: Portfolio,
                                  cfg: dict, recipient: str) -> tuple[Consumer, str]:
    """Returns (consumer, bsuid). Raises 131009 on malformed/cross-portfolio BSUIDs."""
    if PARENT_BSUID_RE.match(recipient):
        account = await portfolio_parent_account(session, portfolio)
        if account is None:
            raise ApiError(131009, "Recipient does not exist or does not belong to this business portfolio "
                                   "(parent BSUID used but this portfolio is not enrolled in a parent account).")
        row = (await session.execute(select(ParentBsuid).where(
            ParentBsuid.value == recipient,
            ParentBsuid.parent_account_id == account.id))).scalar_one_or_none()
        if row:
            consumer = await session.get(Consumer, row.consumer_id)
        else:  # well-formed tester-supplied parent BSUID → auto-create
            consumer = await auto_create_consumer(session, tenant, cfg, country=recipient[:2])
            await get_or_create_parent_bsuid(session, consumer, account, supplied=recipient)
        bsuid = await get_or_create_bsuid(session, consumer, portfolio)
        return consumer, bsuid
    if not BSUID_RE.match(recipient):
        raise ApiError(131009, f"Malformed BSUID {recipient!r}. Expected '<COUNTRY>.<18-20 digits>'.")
    mapping = (await session.execute(
        select(BsuidMapping).join(Portfolio, BsuidMapping.portfolio_id == Portfolio.id)
        .where(BsuidMapping.bsuid == recipient, Portfolio.tenant_id == tenant.id))).scalar_one_or_none()
    if mapping:
        if mapping.portfolio_id != portfolio.id:
            raise ApiError(131009, "Recipient does not exist or does not belong to this business portfolio.")
        return await session.get(Consumer, mapping.consumer_id), mapping.bsuid
    # unknown but well-formed → auto-create from the behavior profile (§3.1)
    consumer = await auto_create_consumer(session, tenant, cfg, country=recipient[:2])
    bsuid = await get_or_create_bsuid(session, consumer, portfolio, supplied=recipient)
    return consumer, bsuid


# ------------------------------------------------------------- username rules

USERNAME_RE = re.compile(r"^[a-z0-9._]{3,35}$", re.IGNORECASE)
DOMAIN_SUFFIXES = (".com", ".org", ".net", ".int", ".edu", ".gov", ".mil", ".us", ".in", ".html")


def validate_username_format(name: str) -> None:
    def bad(reason: str):
        raise ApiError(100, f"Invalid username {name!r}: {reason}")
    if not isinstance(name, str) or not USERNAME_RE.match(name):
        bad("must be 3-35 chars using only a-z, 0-9, '.' and '_'")
    if not re.search(r"[a-zA-Z]", name):
        bad("must contain at least one letter")
    if name.startswith(".") or name.endswith(".") or ".." in name:
        bad("no leading, trailing or consecutive '.'")
    if name.lower().startswith("www"):
        bad("must not start with 'www'")
    if name.lower().endswith(DOMAIN_SUFFIXES):
        bad("must not end in a domain suffix")


async def assert_username_available(session: AsyncSession, name: str,
                                    exclude_bn: str | None = None,
                                    exclude_consumer: str | None = None) -> None:
    low = name.lower()
    q = select(BusinessNumber).where(func.lower(BusinessNumber.username) == low)
    if exclude_bn:
        q = q.where(BusinessNumber.id != exclude_bn)
    if (await session.execute(q)).scalar_one_or_none():
        raise ApiError(147001, f"Username '{name}' is not available.")
    q = select(Consumer).where(func.lower(Consumer.username) == low)
    if exclude_consumer:
        q = q.where(Consumer.id != exclude_consumer)
    if (await session.execute(q)).scalar_one_or_none():
        raise ApiError(147001, f"Username '{name}' is not available.")
