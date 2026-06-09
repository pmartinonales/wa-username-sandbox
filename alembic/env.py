"""Alembic environment. Derives a sync URL from DATABASE_URL (async drivers
are swapped for their sync counterparts) so `alembic upgrade head` works for
both SQLite and Postgres."""
import os

from alembic import context
from sqlalchemy import create_engine

from app.db import Base
from app import models  # noqa: F401 — register all tables on Base.metadata

target_metadata = Base.metadata


def _sync_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/sandbox.db")
    return url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg2")


def run_migrations_offline() -> None:
    context.configure(url=_sync_url(), target_metadata=target_metadata,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
