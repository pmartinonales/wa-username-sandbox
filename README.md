# username-sandbox

A **stateful mock of the 360dialog WhatsApp API** that simulates the WhatsApp
**usernames / BSUID** lifecycle, so partners can test their integrations
before Meta enables usernames globally. No real WhatsApp traffic is involved:
"consumers" are simulated entities that testers create (or that the sandbox
auto-creates) and puppet through a Simulation API.

## Quick start

```bash
docker compose up --build
# in another shell, seed the demo tenant:
docker compose exec sandbox python -m app.seed
```

Or locally:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/alembic upgrade head
.venv/bin/python -m app.seed          # prints all demo keys (deterministic)
.venv/bin/uvicorn app.main:app --reload
```

OpenAPI docs: <http://localhost:8000/docs> — the **Mock API** (partner-facing)
and **Simulation API** (tester control plane) are separate tags.

Run the test suite (covers every behavioral rule in §7–§9 of the spec plus all
15 acceptance scenarios):

```bash
.venv/bin/pytest
```

## Architecture

```
app/
  main.py        FastAPI app, error handler, lifespan (create_all safety net)
  models.py      SQLAlchemy entities (tenant → portfolio → number, consumers,
                 BSUID mappings, contact book, 30-day cache, windows, …)
  rules.py       The rules engine: behavior-config resolution, phone_visible(),
                 contact book & cache mechanics, service windows, BSUID
                 resolution + auto-consumer creation, username validation
  webhooks.py    Webhook payload builders (§9) + async delivery engine with
                 retry/backoff, HMAC signatures, and the delivery log
  ids.py         Deterministic, seedable ID generation (BSUIDs, wamids, keys)
  routers/mock.py     Mock API  (auth: D360-API-KEY, key scopes the number)
  routers/sandbox.py  Simulation API (auth: SANDBOX-API-KEY, tenant-scoped)
  seed.py        Demo tenant per spec §11 (idempotent)
```

* **Two API surfaces, one service.** The Mock API mirrors 360dialog
  conventions (no `phone_number_id` in paths — the API key maps to exactly one
  business number). The Simulation API under `/sandbox/...` is the control
  plane.
* **SQLite via async SQLAlchemy**; Postgres is a config swap
  (`DATABASE_URL=postgresql+asyncpg://...`). No state lives in memory — the
  service survives restarts (one documented exception below).
* **Deterministic IDs.** All BSUIDs, wamids and API keys derive from
  `ID_SEED` + persistent counters, so seeded environments are reproducible.
* **Config-first.** Each number carries a `BehaviorConfig`. Calls referencing
  unknown phones/BSUIDs auto-create consumers from that profile, so testers
  never have to pre-create consumers. Resolution order: explicit per-consumer
  state → key's BehaviorConfig → tenant defaults → real rules engine
  (`"auto"` always falls through to the real rules).
* **Time travel.** Each tenant has a clock offset; `POST /sandbox/time/advance`
  expires 24-hour windows, ages the 30-day cache and the username rate limit.

### Phone-number visibility (the core rule)

Whenever a webhook/response would contain the consumer's phone number:

1. BehaviorConfig `phone_visibility` `"always"`/`"never"` short-circuits.
2. Pre-GA (`ga_mode=false`): always visible.
3. Consumer never adopted a username: always visible.
4. Contact book entry (portfolio-scoped) that knows the phone: visible.
5. 30-day cache entry (**per business number**) that knows the phone: visible.
6. Otherwise: BSUID-only.

The BSUID is always included regardless. Visibility is evaluated **at webhook
delivery time**, so a config flip or a contact-info share affects the very
next status webhook.

## Curl walkthrough (golden path, spec §12.1)

Seed first (`python -m app.seed`) — keys below are the deterministic defaults.

```bash
BASE=http://localhost:8000
SANDBOX_KEY=c6a6fe7cd51374cc8829d2ab9d1fbf3d28d155ae   # demo tenant
API_KEY=a4c79a5822f1e5ce3d87285a86fd6fedd581a19d        # business number bn_000001

# 1. Enable GA mode (post-launch behavior) for the tenant
curl -s -X PATCH $BASE/sandbox/tenants/me \
  -H "SANDBOX-API-KEY: $SANDBOX_KEY" -H "Content-Type: application/json" \
  -d '{"ga_mode": true}'

# 2. Alice (has username, no history with us) sends an inbound message
curl -s -X POST $BASE/sandbox/consumers/cs_000001/send_message \
  -H "SANDBOX-API-KEY: $SANDBOX_KEY" -H "Content-Type: application/json" \
  -d '{"to_business_number_id": "bn_000001", "text": "hi, I need help"}'

# 3. Inspect the webhook we would have received: user_id + username, NO wa_id
curl -s "$BASE/sandbox/webhook_deliveries?page_size=5" \
  -H "SANDBOX-API-KEY: $SANDBOX_KEY" | python3 -m json.tool | tail -40
BSUID=$(curl -s $BASE/sandbox/state/consumers/cs_000001 \
  -H "SANDBOX-API-KEY: $SANDBOX_KEY" | python3 -c \
  'import json,sys;print(json.load(sys.stdin)["bsuids"][0]["bsuid"])')

# 4. Reply by BSUID — response carries user_id (never wa_id) + a wamid
curl -s -X POST $BASE/messages \
  -H "D360-API-KEY: $API_KEY" -H "Content-Type: application/json" \
  -d "{\"recipient\": \"$BSUID\", \"type\": \"text\", \"text\": {\"body\": \"hello Alice\"}}"

# 5. Send the REQUEST_CONTACT_INFO interactive (24h window is open)
WAMID=$(curl -s -X POST $BASE/messages \
  -H "D360-API-KEY: $API_KEY" -H "Content-Type: application/json" \
  -d "{\"recipient\": \"$BSUID\", \"type\": \"interactive\", \"interactive\": {
        \"type\": \"request_contact_info\", \"body\": {\"text\": \"Share your number?\"},
        \"action\": {\"name\": \"request_contact_info\"}}}" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["messages"][0]["id"])')

# 6. Alice taps "Share Contact Info" → contacts webhook (origin=contact_request).
#    The tap requires the message to be *delivered*: with the default status
#    delays that takes ~2s (configure status_delays_ms: [0,0,0] to skip).
sleep 3
curl -s -X POST $BASE/sandbox/consumers/cs_000001/tap_share_contact \
  -H "SANDBOX-API-KEY: $SANDBOX_KEY" -H "Content-Type: application/json" \
  -d "{\"message_wamid\": \"$WAMID\"}"

# 7. Alice messages again — the webhook now includes her phone number
curl -s -X POST $BASE/sandbox/consumers/cs_000001/send_message \
  -H "SANDBOX-API-KEY: $SANDBOX_KEY" -H "Content-Type: application/json" \
  -d '{"to_business_number_id": "bn_000001", "text": "thanks!"}'
curl -s "$BASE/sandbox/webhook_deliveries?page_size=1" \
  -H "SANDBOX-API-KEY: $SANDBOX_KEY" | python3 -m json.tool | tail -30
```

Config-first variant (no consumers at all): create a number with an inline
behavior profile and just start calling the Mock API:

```bash
curl -s -X POST $BASE/sandbox/numbers \
  -H "SANDBOX-API-KEY: $SANDBOX_KEY" -H "Content-Type: application/json" \
  -d '{"portfolio_id": "pf_000001", "behavior": {
        "end_user_has_username": true, "phone_visibility": "never",
        "ga_mode": true, "service_window": "open"}}'
# → returns a fresh D360-API-KEY; POST /messages to any well-formed BSUID
#   (e.g. BR.1234567890123456789) auto-creates the consumer and just works.
```

To receive real callbacks instead of polling the delivery log:

```bash
curl -s -X POST $BASE/configs/webhook -H "D360-API-KEY: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-endpoint.example/webhook", "secret": "shhh"}'
# payloads are signed: X-Sandbox-Signature: sha256=<HMAC-SHA256 of body>
```

## Operational notes

* `POST /sandbox/tenants` creates a tenant + its `SANDBOX-API-KEY`. Set the
  `MASTER_KEY` env var to require an `X-MASTER-KEY` header on this endpoint
  (360dialog ops); unset, it is open (convenient for local use).
* Strict tenant isolation: keys can never read or reach another tenant's data.
  A foreign BSUID used with your key auto-creates a *new* consumer in *your*
  tenant — nothing leaks.
* `inject_error` matches the endpoint keys `messages`, `marketing_messages`
  and `username`.
* **Postgres swap**: set `DATABASE_URL=postgresql+asyncpg://...` and install
  `asyncpg` (plus `psycopg2-binary` for Alembic migrations). No code changes.

## Differences from production (deliberate simplifications)

1. **No real template review** — templates auto-approve instantly (except the
   REQUEST_CONTACT_INFO button validation, which mirrors production subcodes
   2388050/2388153). Unknown template names on send return error `132001`.
2. **Username rate limit returns a dedicated 429** (`131056`) with the
   next-available timestamp — deliberately *correcting* the production bug
   that returns "already claimed". The rate limit counts successful claims
   only; `DELETE /username` does not count as a change.
3. **Pending delayed status webhooks do not survive a restart.** All state is
   in the database, but a `delivered`-in-2s timer in flight during a restart
   is dropped. With the default delays this window is ≤5 s.
4. **Contact book entries written by delivered BSUID sends are flagged
   `phone_known=false`**: they exist (per the spec's §6) but do not grant
   phone visibility — otherwise a single BSUID reply would defeat BSUID-only
   mode, contradicting the golden path. Phone-addressed traffic and
   contact-info shares write `phone_known=true`. Likewise, a BSUID-only
   interaction that refreshes an *expired* cache entry demotes it: it cannot
   resurrect a forgotten phone number.
5. **Migrations**: a single Alembic revision creates the schema from the
   SQLAlchemy models (`alembic upgrade head`); the app's lifespan also runs
   `create_all` as a safety net for ad-hoc runs.
6. **Media, flows, calling, groups, payments, encryption, billing accuracy**
   are out of scope; the `pricing` block in status webhooks is decorative.
   Unsupported message types return a clear `not implemented in sandbox` 400.
7. **`GET /username` returns `{}`** when no username is claimed (the spec only
   says `username` is omitted). Username suggestions derive from the business
   number's `name` (settable when creating the number).
8. **Extra Simulation API endpoints** beyond the spec table:
   `POST /sandbox/messages/{wamid}/force_failed` (force a failed status, §9.2)
   and `DELETE /sandbox/state/consumers/{id}/contact_book/{portfolio_id}`
   (remove *only* the contact book entry, leaving caches intact — lets testers
   stage cache-only visibility, used by acceptance scenario 8; the Mock API's
   `DELETE /contact_book` clears the portfolio's caches as alpha-confirmed).
9. **Auto-created consumers** get a deterministic phone/display name and, if
   the profile sets `parent_bsuid: true` on a portfolio not yet enrolled, a
   parent account is auto-created and enrolled so `parent_user_id` fields
   appear without extra setup.
