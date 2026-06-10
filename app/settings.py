import os

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/sandbox.db")
# Postgres swap: set DATABASE_URL=postgresql+asyncpg://user:pass@host/db

ID_SEED = os.environ.get("ID_SEED", "username-sandbox-seed")

WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "5"))
WEBHOOK_BACKOFF_BASE_S = float(os.environ.get("WEBHOOK_BACKOFF_BASE_S", "0.5"))
WEBHOOK_TIMEOUT_S = float(os.environ.get("WEBHOOK_TIMEOUT_S", "5"))

# POST /sandbox/keys hygiene for the hosted multi-tenant sandbox
KEY_TTL_DAYS = int(os.environ.get("KEY_TTL_DAYS", "90"))
KEYS_PER_IP_PER_HOUR = int(os.environ.get("KEYS_PER_IP_PER_HOUR", "30"))

DEFAULT_STATUS_DELAYS_MS = [
    int(x) for x in os.environ.get("DEFAULT_STATUS_DELAYS_MS", "0,2000,5000").split(",")
]
