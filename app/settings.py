import os

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/sandbox.db")
# Postgres swap: set DATABASE_URL=postgresql+asyncpg://user:pass@host/db

ID_SEED = os.environ.get("ID_SEED", "username-sandbox-seed")

# Optional master key protecting POST /sandbox/tenants (360dialog ops).
MASTER_KEY = os.environ.get("MASTER_KEY")

WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "5"))
WEBHOOK_BACKOFF_BASE_S = float(os.environ.get("WEBHOOK_BACKOFF_BASE_S", "0.5"))
WEBHOOK_TIMEOUT_S = float(os.environ.get("WEBHOOK_TIMEOUT_S", "5"))

DEFAULT_STATUS_DELAYS_MS = [
    int(x) for x in os.environ.get("DEFAULT_STATUS_DELAYS_MS", "0,2000,5000").split(",")
]
