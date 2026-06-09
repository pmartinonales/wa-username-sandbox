"""Deterministic, seedable ID generation.

Every generated identifier is a pure function of (ID_SEED, a persistent
per-prefix counter or stable entity ids), so a sandbox seeded the same way
always produces the same BSUIDs, wamids and API keys.
"""
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


def make_bsuid(country: str, consumer_id: str, portfolio_id: str) -> str:
    return f"{country.upper()}.{digits(19, 'bsuid', consumer_id, portfolio_id)}"


def make_parent_bsuid(country: str, consumer_id: str, account_id: str) -> str:
    return f"{country.upper()}.ENT.{digits(15, 'pbsuid', consumer_id, account_id)}"


def make_api_key(kind: str, n: int) -> str:
    return _h("key", kind, str(n))[:40]


def make_phone(consumer_id: str) -> str:
    return "55" + digits(11, "phone", consumer_id)


def fbtrace_id(*parts: str) -> str:
    return "A" + _h("fbtrace", *parts)[:20]
