"""The rules engine: config resolution/validation (§3.3), phone visibility,
contact-book/cache/window mechanics, identifier resolution, and username
validation. Behavioral rules follow v1 §5–§9."""
import copy
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import ids, settings
from app.errors import ApiError
from app.models import ApiKey, Bsuid, UserState

BSUID_RE = re.compile(r"^[A-Z]{2}\.\d{18,20}$")
PARENT_BSUID_RE = re.compile(r"^[A-Z]{2}\.ENT\.\d+$")

CONFIG_DEFAULTS = {
    "ga_mode": True,
    "user": {
        "has_username": True,
        "username": "auto",
        "country": "BR",
        "phone_visibility": "auto",   # auto | always | never
        "in_contact_book": "auto",    # auto | true | false
        "service_window": "auto",     # auto | open | closed
        "parent_bsuid": False,
    },
    "consumer_actions": {
        "reply_to_messages": False,
        "reply_text": "Hello back!",
        "reply_delay_ms": 3000,
        "tap_request_contact_info": True,
        "tap_delay_ms": 3000,
        "share_contact_manually": False,
    },
    "statuses": {
        "sequence": ["sent", "delivered", "read"],
        "delays_ms": settings.DEFAULT_STATUS_DELAYS_MS,
        "failed_error_code": 131049,
    },
    "inject_error": None,
}

_ENUMS = {
    ("user", "phone_visibility"): {"auto", "always", "never"},
    ("user", "service_window"): {"auto", "open", "closed"},
}
_STATUSES = {"sent", "delivered", "read", "failed"}


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate_config_patch(patch: dict) -> dict:
    def bad(msg: str):
        raise ApiError(100, f"Invalid config: {msg}")

    if not isinstance(patch, dict):
        bad("body must be a JSON object")
    unknown = set(patch) - set(CONFIG_DEFAULTS)
    if unknown:
        bad(f"unknown keys {sorted(unknown)}")
    for section in ("user", "consumer_actions", "statuses"):
        if section in patch:
            if not isinstance(patch[section], dict):
                bad(f"'{section}' must be an object")
            unknown = set(patch[section]) - set(CONFIG_DEFAULTS[section])
            if unknown:
                bad(f"unknown keys in '{section}': {sorted(unknown)}")
    for (section, field), allowed in _ENUMS.items():
        v = patch.get(section, {}).get(field)
        if v is not None and v not in allowed:
            bad(f"{section}.{field} must be one of {sorted(allowed)}")
    icb = patch.get("user", {}).get("in_contact_book")
    if icb not in (None, "auto", True, False):
        bad("user.in_contact_book must be 'auto', true or false")
    seq = patch.get("statuses", {}).get("sequence")
    if seq is not None and (not isinstance(seq, list) or not set(seq) <= _STATUSES):
        bad(f"statuses.sequence entries must be in {sorted(_STATUSES)}")
    ie = patch.get("inject_error")
    if ie is not None and not (isinstance(ie, dict) and "on" in ie and "code" in ie):
        bad('inject_error must be null or {"on": ..., "code": ..., "times": N}')
    return patch


def effective_config(key: ApiKey) -> dict:
    return deep_merge(CONFIG_DEFAULTS, key.config or {})


def user_username(key: ApiKey, cfg: dict | None = None) -> str | None:
    """The simulated user's username per config (None when not adopted)."""
    cfg = cfg or effective_config(key)
    if not cfg["user"]["has_username"]:
        return None
    explicit = cfg["user"]["username"]
    return explicit if explicit != "auto" else ids.derived_username(key.id)


async def pop_injected_error(session: AsyncSession, key: ApiKey, endpoint: str) -> None:
    """§3.3 inject_error: fail the next N matching API calls. The decrement is
    committed before raising so the error fires exactly N times."""
    ie = (key.config or {}).get("inject_error")
    if not ie or ie.get("on") != endpoint or ie.get("times", 1) <= 0:
        return
    ie["times"] = ie.get("times", 1) - 1
    if ie["times"] <= 0:
        key.config["inject_error"] = None
    flag_modified(key, "config")
    await session.commit()
    raise ApiError(int(ie["code"]),
                   f"Sandbox-injected error on '{endpoint}' (config inject_error).")


# ---------------------------------------------------------------- visibility

def phone_visible(key: ApiKey, cfg: dict | None = None) -> bool:
    """v1 §5, against the key's single simulated user."""
    cfg = cfg or effective_config(key)
    user = key.user
    vis = cfg["user"]["phone_visibility"]
    if vis == "always":
        return True
    if vis == "never":
        return False
    if not cfg["ga_mode"]:
        return True  # pre-GA: phones always visible
    if not cfg["user"]["has_username"]:
        return True  # user never adopted a username
    icb = cfg["user"]["in_contact_book"]
    if icb is True:
        return True  # forced contact-book entry
    if icb == "auto" and user.contact_book and user.contact_book_phone_known:
        return True
    if (user.cache_at and user.cache_phone_known
            and user.cache_at > now() - timedelta(days=30)):
        return True  # 30-day cache (per business number == per key)
    return False


def touch_contact(user: UserState, when: datetime, phone_known: bool = True) -> None:
    """v1 §6 write rules. phone_known=False entries (delivered BSUID sends)
    exist but never grant visibility; refreshing an expired cache entry from a
    BSUID-only interaction demotes it (can't resurrect a forgotten phone)."""
    if not user.contact_book:
        user.contact_book = True
        user.contact_book_phone_known = phone_known
    elif phone_known:
        user.contact_book_phone_known = True
    if user.cache_at is None:
        user.cache_phone_known = phone_known
    elif phone_known:
        user.cache_phone_known = True
    elif user.cache_at < when - timedelta(days=30):
        user.cache_phone_known = False
    user.cache_at = when


# ------------------------------------------------------------ service window

def window_open(key: ApiKey, cfg: dict | None = None) -> bool:
    cfg = cfg or effective_config(key)
    sw = cfg["user"]["service_window"]
    if sw == "open":
        return True
    if sw == "closed":
        return False
    opened = key.user.window_opened_at
    return opened is not None and opened > now() - timedelta(hours=24)


# --------------------------------------------------- identifier resolution

async def _retire_bsuids(session: AsyncSession, key: ApiKey) -> None:
    rows = (await session.execute(select(Bsuid).where(
        Bsuid.api_key_id == key.id, Bsuid.status == "active"))).scalars().all()
    for r in rows:
        r.status = "retired"


async def _register_bsuid(session: AsyncSession, key: ApiKey, value: str,
                          origin: str = "generated") -> None:
    existing = (await session.execute(select(Bsuid).where(
        Bsuid.api_key_id == key.id, Bsuid.value == value))).scalar_one_or_none()
    if existing:
        existing.status = "active"
    else:
        session.add(Bsuid(id=await ids.new_id(session, "bs"), api_key_id=key.id,
                          value=value, status="active", origin=origin))


async def resolve_phone(session: AsyncSession, key: ApiKey, to: str) -> tuple[UserState, bool]:
    """Attach a phone send to the simulated user. A different phone after prior
    phone traffic is a simulated *phone change* (v1 §7.1): the BSUID regenerates
    and the caller must emit the system webhook. Returns (user, phone_changed)."""
    phone = re.sub(r"[^\d]", "", str(to))
    if not phone:
        raise ApiError(131009, f"Invalid phone number: {to!r}")
    user = key.user
    changed = False
    if phone != user.phone:
        if user.had_phone_traffic:
            changed = True
            await _retire_bsuids(session, key)
            user.bsuid = ids.make_bsuid(user.country, key.id, phone)
            await _register_bsuid(session, key, user.bsuid)
        user.phone = phone
    user.had_phone_traffic = True
    return user, changed


async def resolve_bsuid(session: AsyncSession, key: ApiKey, cfg: dict,
                        recipient: str) -> UserState:
    """Attach a BSUID send to the simulated user. Well-formed unknown values are
    adopted (auto-creation, v1 §3.1); foreign or retired values → 131009."""
    user = key.user
    if PARENT_BSUID_RE.match(recipient):
        if not cfg["user"]["parent_bsuid"]:
            raise ApiError(131009, "Recipient does not exist or does not belong to this "
                                   "business portfolio (parent BSUIDs require "
                                   "user.parent_bsuid=true in the sandbox config).")
        user.parent_bsuid = recipient
        return user
    if not BSUID_RE.match(recipient):
        raise ApiError(131009, f"Malformed BSUID {recipient!r}. "
                               "Expected '<COUNTRY>.<18-20 digits>'.")
    own = (await session.execute(select(Bsuid).where(
        Bsuid.api_key_id == key.id, Bsuid.value == recipient))).scalars().first()
    if own is not None:
        if own.status == "retired":
            raise ApiError(131009, "Recipient does not exist or does not belong to this "
                                   "business portfolio (the user changed their phone "
                                   "number; this BSUID was regenerated).")
        user.bsuid = recipient
        return user
    # a value the sandbox GENERATED for another key is that portfolio's BSUID —
    # using it here is the cross-portfolio mistake production rejects
    foreign = (await session.execute(select(Bsuid).where(
        Bsuid.value == recipient, Bsuid.origin == "generated"))).scalars().first()
    if foreign is not None:
        raise ApiError(131009, "Recipient does not exist or does not belong to this "
                               "business portfolio.")
    # unknown (or tester-supplied elsewhere) but well-formed → adopt; BSUIDs are
    # portfolio-scoped, so the same supplied digits may live under many keys
    await _register_bsuid(session, key, recipient, origin="supplied")
    user.bsuid = recipient
    return user


def parent_id(key: ApiKey, cfg: dict | None = None) -> str | None:
    cfg = cfg or effective_config(key)
    return key.user.parent_bsuid if cfg["user"]["parent_bsuid"] else None


# ------------------------------------------------------------- username rules

USERNAME_RE = re.compile(r"^[a-z0-9._]{3,35}$", re.IGNORECASE)
DOMAIN_SUFFIXES = (".com", ".org", ".net", ".int", ".edu", ".gov", ".mil", ".us", ".in", ".html")


def validate_username_format(name) -> None:
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
                                    exclude_key: str | None = None) -> None:
    """Global, case-insensitive uniqueness across claimed business usernames."""
    q = select(ApiKey).where(func.lower(ApiKey.username) == name.lower())
    if exclude_key:
        q = q.where(ApiKey.id != exclude_key)
    if (await session.execute(q)).scalars().first():
        raise ApiError(147001, f"Username '{name}' is not available.")
