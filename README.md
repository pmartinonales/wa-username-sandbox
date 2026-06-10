# username-sandbox (v2)

A stateful mock of the 360dialog WhatsApp API that simulates the
**usernames / BSUID** lifecycle: consumers hiding their phone number behind a
handle, Business-Scoped User IDs, the Meta contact book, the 30-day phone
cache, contact-info request flows, and business username management.

**The sandbox looks exactly like the API Meta is releasing, plus exactly three
sandbox-specific endpoints:**

```
1. POST /sandbox/keys                → get a D360-API-KEY
2. PUT  /sandbox/webhook             → point webhooks at my endpoint
3. PUT  /sandbox/config              → choose the behavior I want to see
4. ...then call only real API endpoints (/messages, /username, ...)
```

Every API key owns one virtual business phone number (with its own implicit
portfolio) and **one simulated consumer counterpart**. The config describes
"the user I am currently talking to" — flip it between calls, or create a
second key for a second user. There are no tenants, no consumer-management
endpoints, no time-travel: everything is driven by configuration and automatic
simulated-consumer behavior.

## Run it

```bash
docker compose up --build          # http://localhost:8000
```

or locally:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/uvicorn app.main:app --reload
```

- Interactive API docs: **http://localhost:8000/docs**
- Webhook payload reference: **http://localhost:8000/docs/webhooks** (also [WEBHOOKS.md](WEBHOOKS.md))
- Tests: `.venv/bin/pytest`

## Dashboard UI

A Next.js + shadcn/ui dashboard lives in [`ui/`](ui/): create API keys, flip
every behavior switch/selector, send test messages, and watch a live feed of
API requests (with bodies and error codes) and webhook deliveries. No UI auth —
access is scoped by whichever `D360-API-KEY` you create or paste (stored in
localStorage).

```bash
cd ui && npm install && npm run dev    # http://localhost:3000
```

Point it at the API with the "API base URL" field in the header (default
`http://localhost:8000`, override at build time with `NEXT_PUBLIC_API_BASE`).

**Deploy on Vercel:** import the repo, set the project **Root Directory** to
`ui`, and add an env var `NEXT_PUBLIC_API_BASE=https://<your-hosted-sandbox>`.
The API serves CORS for any origin, so no proxy is needed. The monitoring feed
uses a schema-hidden `GET /sandbox/requests` endpoint, so the documented
sandbox surface stays exactly three endpoints.

## The five-minute path

```bash
BASE=${BASE:-http://localhost:8000}

# 1. Get a key
KEY=$(curl -s -X POST $BASE/sandbox/keys -H 'Content-Type: application/json' \
  -d '{"name":"my-test"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')

# 2. Point webhooks at your endpoint (skip this and use GET /sandbox/webhook as your log)
curl -s -X PUT $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" \
  -H 'Content-Type: application/json' -d "{\"url\":\"$WEBHOOK_URL\"}"

# 3. Choose the behavior: username user, phone never visible, auto-replies
curl -s -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' -d '{
  "user": {"has_username": true, "phone_visibility": "never"},
  "consumer_actions": {"reply_to_messages": true, "reply_delay_ms": 500}
}'

# 4. Call the real API — send to any well-formed BSUID; it attaches to your simulated user
curl -s -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"Hi!"}}'
```

~0.5s later your endpoint receives BSUID-only status webhooks and a BSUID-only
inbound reply — the exact payloads your production code must survive.

> Note: had you sent **to a phone number** instead, the status webhooks would
> include `recipient_id`/`wa_id` regardless of `phone_visibility` — per Meta's
> identifier quick reference, phone-addressed messages always echo the phone
> the business itself supplied. Visibility rules gate BSUID-addressed traffic
> and inbound/contacts webhooks.

## How the simulation works

- **One simulated user per key.** Sends to any well-formed phone number or
  BSUID are accepted and attach to this user. Malformed BSUIDs → `131009`.
  A BSUID belonging to another key → `131009` (cross-portfolio scoping).
- **`"auto"` always means "apply the real production rules"** — contact book,
  30-day per-number cache, 24h service window, pre/post-GA. Forced values
  (`"always"`, `"never"`, `true`, `false`, `"open"`, `"closed"`) override them.
- **The window starts open**: the simulated user "opened the conversation"
  when the key was created. It stays open 24h and refreshes on every simulated
  inbound message. Force it with `user.service_window`.
- **`consumer_actions`** replace a puppeting API: after each *delivered*
  outbound message the user can reply (`reply_to_messages`), tap a
  Share-Contact-Info button (`tap_request_contact_info`), or share their card
  manually (`share_contact_manually`).
- **User-initiated messages:** `POST /sandbox/inbound` (D360-API-KEY auth)
  makes the simulated user message *you*, unprompted — opening the 24h window
  and emitting the inbound webhook with whatever identity state the config
  describes. Optional body: `{"text": "...", "config": {<same shape as PUT
  /sandbox/config, applied first>}}`, so "user *without* a username who *is*
  in my contact book says hi" is one call; `{"type": "contacts", "origin":
  "other"|"contact_request"}` triggers a contact-card share instead. (Like
  `GET /sandbox/requests`, it's hidden from the OpenAPI schema: the documented
  sandbox surface stays at three endpoints.)
- **Phone change simulation:** sending to a *different* phone number after
  prior phone traffic = the user changed their phone. BSUIDs regenerate (old
  ones → `131009`) and a `system` webhook is emitted.
- Webhooks are recorded even with no URL configured — `GET /sandbox/webhook`
  returns the last 50 attempts with payloads. That's your debugging tool.

---

# Validate your integration, use case by use case

Every block below is copy-pasteable end to end — only `BASE` (sandbox host)
and `WEBHOOK_URL` (your endpoint, optional) need substituting. Each block
creates its own key. The cookbook sets short webhook delays so you see results
immediately; defaults are production-like (~2s/5s).

### 1. Receive a BSUID-only inbound message

**Goal:** parse `user_id`, `from_user_id`, `username`; survive missing
`wa_id`/`from`.

**Config:** username user, phone never visible, auto-reply on.

<!-- usecase:1 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' -d '{
  "user": {"has_username": true, "phone_visibility": "never"},
  "consumer_actions": {"reply_to_messages": true, "reply_delay_ms": 0},
  "statuses": {"delays_ms": [0, 0, 0]}
}' > /dev/null
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"to":"5511988880001","type":"text","text":{"body":"Anyone there?"}}' > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
vals = [x["payload"]["entry"][0]["changes"][0]["value"] for x in d]
inbound = [v for v in vals if "messages" in v and "statuses" not in v
           and v["messages"][0]["type"] == "text"][0]
msg, contact = inbound["messages"][0], inbound["contacts"][0]
assert "from" not in msg and "wa_id" not in contact, "phone leaked!"
assert msg["from_user_id"] and contact["user_id"], "BSUID missing!"
assert contact["profile"]["username"], "username missing!"
print("OK: inbound is BSUID-only, username =", contact["profile"]["username"])'
echo "KEY=$KEY"
```

**You should observe:** an inbound webhook whose `contacts[0]` has `user_id` +
`profile.username` but **no `wa_id`**, and whose `messages[0]` has
`from_user_id` but **no `from`**.

**Your integration passes when** it keys the conversation on
`from_user_id`/`user_id` and renders the username — without ever dereferencing
a phone field.

### 2. Reply to a BSUID

**Goal:** handle the `{input, user_id}` send response; store the wamid.

<!-- usecase:2 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' -d '{
  "consumer_actions": {"reply_to_messages": true, "reply_delay_ms": 0},
  "statuses": {"delays_ms": [0, 0, 0]}
}' > /dev/null
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"to":"5511988880001","type":"text","text":{"body":"hello"}}' > /dev/null
sleep 1
BSUID=$(curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
v = [x["payload"]["entry"][0]["changes"][0]["value"] for x in d]
print([x for x in v if "messages" in x and x["messages"][0].get("from_user_id")][0]["messages"][0]["from_user_id"])')
echo "User BSUID: $BSUID"
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d "{\"recipient\":\"$BSUID\",\"type\":\"text\",\"text\":{\"body\":\"replying to your BSUID\"}}" \
  | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert r["contacts"][0]["user_id"] and "wa_id" not in r["contacts"][0]
assert r["messages"][0]["id"].startswith("wamid.")
print("OK: response =", json.dumps(r))'
echo "KEY=$KEY"
```

**You should observe:** `{"contacts":[{"input":"<BSUID>","user_id":"<BSUID>"}],
"messages":[{"id":"wamid..."}]}` — `user_id`, never `wa_id`, and always a wamid.

**Your integration passes when** it reads `user_id` (not `wa_id`) from BSUID
send responses and persists the wamid for status correlation.

### 3. Correlate statuses by wamid with no phone present

<!-- usecase:3 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"user": {"phone_visibility": "never"}, "statuses": {"delays_ms": [0, 0, 0]}}' > /dev/null
WAMID=$(curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"track me"}}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["messages"][0]["id"])')
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c "
import json, sys
d = json.load(sys.stdin)['deliveries']
statuses = [x['payload']['entry'][0]['changes'][0]['value']['statuses'][0]
            for x in d if 'statuses' in x['payload']['entry'][0]['changes'][0]['value']]
mine = [s for s in statuses if s['id'] == '$WAMID']
assert {s['status'] for s in mine} == {'sent', 'delivered', 'read'}, mine
assert all('recipient_id' not in s and s['recipient_user_id'] for s in mine)
print('OK:', len(mine), 'statuses correlated by wamid, all BSUID-only')"
echo "KEY=$KEY"
```

**You should observe:** `sent`/`delivered`/`read` statuses carrying your wamid
in `statuses[0].id` and `recipient_user_id`, with **no `recipient_id`**.

**Your integration passes when** delivery state is keyed on the wamid alone.

### 4. Send with both `to` + `recipient` — phone precedence

<!-- usecase:4 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"to":"5511988880001","recipient":"BR.13491208655302741918","type":"text","text":{"body":"both"}}' \
  | python3 -c '
import json, sys
r = json.load(sys.stdin)
c = r["contacts"][0]
assert c["wa_id"] == "5511988880001" and "user_id" not in c, c
print("OK: to wins →", json.dumps(c))'
echo "KEY=$KEY"
```

**You should observe:** the response has `wa_id` and **no `user_id`** — the
phone won.

### 5. Closed 24h window on BSUID send → `131047` → template fallback

<!-- usecase:5 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"user": {"service_window": "closed"}, "statuses": {"delays_ms": [0, 0, 0]}}' > /dev/null
curl -s -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"free-form"}}' \
  | python3 -c '
import json, sys
e = json.load(sys.stdin)["error"]
assert e["code"] == 131047, e
print("OK: got 131047 —", e["error_data"]["details"])'
curl -sf -X POST $BASE/v1/configs/templates -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"reengage","language":"en","category":"utility","components":[{"type":"BODY","text":"We miss you"}]}' > /dev/null
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"template","template":{"name":"reengage","language":{"code":"en"}}}' \
  | python3 -c 'import json,sys;r=json.load(sys.stdin);assert r["messages"][0]["id"];print("OK: template fallback succeeded")'
echo "KEY=$KEY"
```

**Your integration passes when** a `131047` on a free-form send triggers a
template fallback (templates always pass the window check).

### 6. Auth template to BSUID → `131062` → phone fallback

<!-- usecase:6 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"statuses": {"delays_ms": [0, 0, 0]}}' > /dev/null
curl -sf -X POST $BASE/v1/configs/templates -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"login_code","language":"en","category":"utility","auth_flavor":"one-tap","components":[{"type":"BODY","text":"Your code: {{1}}"}]}' > /dev/null
curl -s -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"template","template":{"name":"login_code","language":{"code":"en"}}}' \
  | python3 -c '
import json, sys
e = json.load(sys.stdin)["error"]
assert e["code"] == 131062, e
print("OK: got 131062 —", e["error_data"]["details"])'
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"to":"5511988880001","type":"template","template":{"name":"login_code","language":{"code":"en"}}}' \
  | python3 -c 'import json,sys;r=json.load(sys.stdin);assert r["messages"][0]["id"];print("OK: same template to phone succeeded")'
echo "KEY=$KEY"
```

**Your integration passes when** auth templates are only ever dispatched to
phone-addressed recipients, with `131062` handled as the trigger to fall back.

### 7. Recover a phone number via REQUEST_CONTACT_INFO

**Goal:** create the template + interactive, receive the tap (contacts
webhook, `origin=contact_request`, **no vCard**), confirm the phone reappears.

<!-- usecase:7 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' -d '{
  "consumer_actions": {"tap_request_contact_info": true, "tap_delay_ms": 0},
  "statuses": {"delays_ms": [0, 0, 0]}
}' > /dev/null
curl -sf -X POST $BASE/v1/configs/templates -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"recover_phone","language":"en","category":"utility","components":[{"type":"BODY","text":"Share your number?"},{"type":"BUTTONS","buttons":[{"type":"REQUEST_CONTACT_INFO","text":"Share Contact Info"}]}]}' > /dev/null
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"interactive","interactive":{"type":"request_contact_info","body":{"text":"Please share"},"action":{"name":"request_contact_info"}}}' > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
vals = [x["payload"]["entry"][0]["changes"][0]["value"] for x in d]
shares = [v for v in vals if "messages" in v and v["messages"][0]["type"] == "contacts"]
assert shares, "no contacts webhook"
share = shares[0]["messages"][0]["contacts"][0]
assert share["origin"] == "contact_request" and "vcard" not in share, share
print("OK: tap received, phone =", share["phones"][0]["phone"], "(no vCard)")'
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"thanks!"}}' > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
statuses = [x["payload"]["entry"][0]["changes"][0]["value"]["statuses"][0]
            for x in d if "statuses" in x["payload"]["entry"][0]["changes"][0]["value"]]
latest = statuses[0]
assert latest.get("recipient_id"), latest
print("OK: phone is back in statuses:", latest["recipient_id"])'
echo "KEY=$KEY"
```

**Your integration passes when** it parses the `contact_request` share (no
vCard!) and updates its contact record so later webhooks with `wa_id` correlate.

### 8. Manual contact share — `origin=other`, vCard present

<!-- usecase:8 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' -d '{
  "consumer_actions": {"share_contact_manually": true, "tap_delay_ms": 0},
  "statuses": {"delays_ms": [0, 0, 0]}
}' > /dev/null
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"hi"}}' > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
vals = [x["payload"]["entry"][0]["changes"][0]["value"] for x in d]
shares = [v for v in vals if "messages" in v and v["messages"][0]["type"] == "contacts"]
share = shares[0]["messages"][0]["contacts"][0]
assert share["origin"] == "other" and share["vcard"].startswith("BEGIN:VCARD"), share
print("OK: manual share with vCard received")'
echo "KEY=$KEY"
```

**Your integration passes when** it parses **both** share variants: with vCard
(`origin=other`) and without (`origin=contact_request`).

### 9. Claim a business username, hit the rate limit

<!-- usecase:9 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys -H 'Content-Type: application/json' \
  -d '{"name":"acme"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf $BASE/username_suggestions -H "D360-API-KEY: $KEY"
echo
SUFFIX=$RANDOM$RANDOM
for i in 1 2 3; do
  curl -sf -X POST $BASE/username -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
    -d "{\"username\":\"acme.try$i.x$SUFFIX\"}"
  echo
done
curl -s -X POST $BASE/username -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d "{\"username\":\"acme.try4.x$SUFFIX\"}" | python3 -c '
import json, sys
e = json.load(sys.stdin)["error"]
assert e["code"] == 131056, e
assert "Next change available at" in e["error_data"]["details"]
print("OK: dedicated rate-limit error —", e["error_data"]["details"])'
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
ups = [x for x in d if x["field"] == "business_username_update"]
assert len(ups) == 3, len(ups)
print("OK:", len(ups), "business_username_update webhooks received")'
echo "KEY=$KEY"
```

**Your integration passes when** it treats HTTP 429 / code `131056` as
*rate-limited, retry after the timestamp in details* — **not** as
"name already claimed" (`147001` is a different error).

### 10. Handle `failed` statuses (no `contacts` array)

<!-- usecase:10 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"statuses": {"sequence": ["sent", "failed"], "delays_ms": [0, 0], "failed_error_code": 131026}}' > /dev/null
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"will fail"}}' > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
vals = [x["payload"]["entry"][0]["changes"][0]["value"]
        for x in d if "statuses" in x["payload"]["entry"][0]["changes"][0]["value"]]
failed = [v for v in vals if v["statuses"][0]["status"] == "failed"][0]
assert "contacts" not in failed, failed
assert failed["statuses"][0]["errors"][0]["code"] == 131026
print("OK: failed status has errors[] and no contacts array")'
echo "KEY=$KEY"
```

**Your integration passes when** failed-status handling never assumes
`contacts` exists and reads `statuses[0].errors[]`.

### 11. Pre-GA vs GA: flip `ga_mode`

<!-- usecase:11 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"ga_mode": false, "statuses": {"delays_ms": [0, 0, 0]}}' > /dev/null
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"pre-GA"}}' > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
s = [x["payload"]["entry"][0]["changes"][0]["value"]["statuses"][0]
     for x in d if "statuses" in x["payload"]["entry"][0]["changes"][0]["value"]][0]
assert s.get("recipient_id"), s
print("OK pre-GA: phone present even on BSUID send:", s["recipient_id"])'
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"ga_mode": true, "user": {"phone_visibility": "never"}}' > /dev/null
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"post-GA"}}' > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
s = [x["payload"]["entry"][0]["changes"][0]["value"]["statuses"][0]
     for x in d if "statuses" in x["payload"]["entry"][0]["changes"][0]["value"]][0]
assert "recipient_id" not in s and s["recipient_user_id"], s
print("OK post-GA: BSUID-only")'
echo "KEY=$KEY"
```

**Your integration passes when** the same code path works in both worlds —
today (phones everywhere) and post-GA (phones can vanish).

### 12. Contact book deletion → BSUID-only, inbound still arrives

<!-- usecase:12 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' -d '{
  "consumer_actions": {"reply_to_messages": true, "reply_delay_ms": 0},
  "statuses": {"delays_ms": [0, 0, 0]}
}' > /dev/null
# phone send → contact book entry written → phone visible
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"to":"5511988880001","type":"text","text":{"body":"hello"}}' > /dev/null
sleep 1
BSUID=$(curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
v = [x["payload"]["entry"][0]["changes"][0]["value"] for x in d]
print([x for x in v if "statuses" in x][0]["statuses"][0]["recipient_user_id"])')
curl -sf -X DELETE "$BASE/contact_book?messaging_product=whatsapp&bsuid=$BSUID" \
  -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert r["success"] and r["deleted"], r
print("OK: contact book entry deleted")'
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d "{\"recipient\":\"$BSUID\",\"type\":\"text\",\"text\":{\"body\":\"still there?\"}}" > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
vals = [x["payload"]["entry"][0]["changes"][0]["value"] for x in d]
statuses = [v["statuses"][0] for v in vals if "statuses" in v]
assert "recipient_id" not in statuses[0], statuses[0]
inbound = [v for v in vals if "messages" in v and "statuses" not in v
           and v["messages"][0]["type"] == "text"]
assert inbound, "inbound reply missing — the alpha webhook-drop bug!"
assert "from" not in inbound[0]["messages"][0]
print("OK: BSUID-only after delete; inbound webhooks still arriving")'
echo "KEY=$KEY"
```

**Your integration passes when** it keeps the conversation alive on the BSUID
after the phone disappears mid-conversation.

### 13. Parent BSUIDs

<!-- usecase:13 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"user": {"parent_bsuid": true}, "statuses": {"delays_ms": [0, 0, 0]}}' > /dev/null
curl -sf $BASE/parent-bsuid-accounts -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert r["parent_bsuid_account_id"] and r["enrolled_business_portfolios"], r
print("OK: parent account", r["parent_bsuid_account_id"])'
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"recipient":"BR.13491208655302741918","type":"text","text":{"body":"hi"}}' > /dev/null
sleep 1
curl -sf $BASE/sandbox/webhook -H "D360-API-KEY: $KEY" | python3 -c '
import json, sys
d = json.load(sys.stdin)["deliveries"]
v = [x["payload"]["entry"][0]["changes"][0]["value"]
     for x in d if "statuses" in x["payload"]["entry"][0]["changes"][0]["value"]][0]
assert v["statuses"][0]["recipient_parent_user_id"].split(".")[1] == "ENT"
assert v["contacts"][0]["parent_user_id"]
print("OK: parent_user_id + recipient_parent_user_id present")'
echo "KEY=$KEY"
```

**Your integration passes when** it stores `parent_user_id` alongside
`user_id` and treats `<COUNTRY>.ENT.<digits>` as a distinct identifier type.

### 14. Error resilience with `inject_error`

<!-- usecase:14 -->
```bash
BASE=${BASE:-http://localhost:8000}
KEY=$(curl -s -X POST $BASE/sandbox/keys | python3 -c 'import json,sys;print(json.load(sys.stdin)["d360_api_key"])')
curl -sf -X PUT $BASE/sandbox/config -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"inject_error": {"on": "messages", "code": 131047, "times": 1}, "statuses": {"delays_ms": [0, 0, 0]}}' > /dev/null
curl -s -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"to":"5511988880001","type":"text","text":{"body":"first"}}' | python3 -c '
import json, sys
e = json.load(sys.stdin)["error"]
assert e["code"] == 131047, e
print("OK: injected 131047")'
curl -sf -X POST $BASE/messages -H "D360-API-KEY: $KEY" -H 'Content-Type: application/json' \
  -d '{"to":"5511988880001","type":"text","text":{"body":"second"}}' | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert r["messages"][0]["id"], r
print("OK: next call succeeded — injection consumed")'
echo "KEY=$KEY"
```

**Your integration passes when** the injected error exercises your retry /
alerting path and normal operation resumes on the next call.

---

## Architecture

```
app/
  main.py        FastAPI app, /docs/webhooks, error envelope handler
  settings.py    env config (DATABASE_URL, key TTL, webhook retries, ...)
  db.py          async SQLAlchemy engine/session (SQLite ↔ Postgres swap)
  models.py      ApiKey, UserState, Bsuid, Template, Message,
                 WebhookDelivery, UsernameChange, Sequence
  ids.py         deterministic, seedable ID generation (ID_SEED env)
  rules.py       THE rules engine: config resolution, phone visibility,
                 contact book/cache/window, BSUID scoping, username rules
  webhooks.py    payload builders, delivery w/ retries + HMAC,
                 status lifecycle, simulated consumer actions
  routers/
    sandbox.py   the 3 sandbox endpoints
    mock.py      the real API surface
```

State lives entirely in the database (`data/sandbox.db`; set `DATABASE_URL`
for Postgres). One exception: **pending delayed webhooks** (statuses,
consumer actions) are asyncio tasks and do not survive a restart.

## Differences from production

Deliberate simplifications, all documented in code:

1. **No real WhatsApp traffic** — consumers are simulated; one per key.
2. **The 24h window starts open** at key creation so the five-minute path
   works without a prior inbound. Production requires a real consumer message
   first. Force `user.service_window: "closed"` to test re-engagement.
3. **Delivered BSUID sends write contact book/cache entries flagged
   `phone_known=false`** — they exist (per alpha behavior) but never reveal
   the phone; otherwise a single BSUID reply would defeat BSUID-only mode.
4. **Username uniqueness** is enforced across business usernames only;
   simulated-user usernames don't reserve names.
5. **Username rate-limit window** counts successful `POST /username` calls;
   `DELETE` doesn't count.
6. **Templates** auto-approve; only REQUEST_CONTACT_INFO button rules are
   validated. Media/flows/calling/groups/payments are out of scope.
7. **Pricing blocks are decorative**; conversation ids are hashes of the wamid.
8. **Migrations** are model-driven (`alembic upgrade head` runs `create_all`).
   v2 is a breaking schema change: delete `data/sandbox.db` from v1.
9. **Phone-change simulation** (sending to a new phone after prior phone
   traffic) is a sandbox convention to trigger the `system` webhook + BSUID
   regeneration; old BSUIDs then return `131009`.

## Testing

```bash
.venv/bin/pytest                          # full suite
.venv/bin/pytest tests/test_cookbook.py   # README cookbook blocks, run verbatim
```

`tests/test_cookbook.py` extracts every `<!-- usecase:N -->` bash block from
this README and executes it against a live server instance — the cookbook
cannot rot.
