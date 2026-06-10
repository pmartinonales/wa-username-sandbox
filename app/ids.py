"""Deterministic, seedable ID generation: every identifier is a pure function
of (ID_SEED, persistent counters or stable entity ids), so a sandbox seeded the
same way always produces the same keys, BSUIDs and wamids."""
import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings
from app.models import Sequence


def _h(*parts: str) -> str:
    return hashlib.sha256(":".join([settings.ID_SEED, *map(str, parts)]).encode()).hexdigest()


def digits(n: int, *parts: str) -> str:
    out = ""
    i = 0
    while len(out) < n:
        out += str(int(_h(*parts, str(i)), 16))
        i += 1
    d = out[:n]
    return ("1" + d[1:]) if d[0] == "0" else d  # no leading zero


async def next_seq(session: AsyncSession, name: str) -> int:
    seq = await session.get(Sequence, name)
    if seq is None:
        seq = Sequence(name=name, value=0)
        session.add(seq)
    seq.value += 1
    await session.flush()
    return seq.value


async def new_id(session: AsyncSession, prefix: str) -> str:
    n = await next_seq(session, prefix)
    return f"{prefix}_{n:06d}"


async def new_wamid(session: AsyncSession) -> str:
    n = await next_seq(session, "wamid")
    return "wamid.HBg" + _h("wamid", str(n))[:40].upper() + "="


def make_api_key(n: int) -> str:
    return "sk_sandbox_" + _h("key", str(n))[:32]


COUNTRY_PREFIX = {"BR": "5511", "US": "1212", "DE": "4930", "IN": "9111", "GB": "4420"}


def business_phone(country: str, n: int) -> str:
    return COUNTRY_PREFIX.get(country.upper(), "5511") + "90" + f"{n:07d}"


def waba_id(n: int) -> str:
    return "1" + f"{n:014d}"


def phone_number_id(n: int) -> str:
    return "2" + f"{n:014d}"


def portfolio_id(n: int) -> str:
    return "3" + f"{n:014d}"


def user_phone(country: str, key_id: str) -> str:
    return COUNTRY_PREFIX.get(country.upper(), "5511") + "8" + digits(8, "userphone", key_id)


def make_bsuid(country: str, key_id: str, phone: str) -> str:
    return f"{country.upper()}.{digits(19, 'bsuid', key_id, phone)}"


def make_parent_bsuid(country: str, key_id: str) -> str:
    return f"{country.upper()}.ENT.{digits(15, 'pbsuid', key_id)}"


def derived_username(key_id: str) -> str:
    return f"user.{digits(8, 'uname', key_id)}"


def fbtrace_id(*parts: str) -> str:
    return "A" + _h("fbtrace", *parts)[:20]
