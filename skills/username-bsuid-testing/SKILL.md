---
name: username-bsuid-testing
description: >
  Expert assistant for WhatsApp's usernames/BSUID rollout and the
  username-sandbox testing environment. Knows every rule (phone visibility,
  contact book, 30-day cache, 24h service window, BSUID scoping, username
  format/rate limits) and every error code (131009, 131047, 131056, 131062,
  147001, 100, 132001, 2388050/2388153), and drives the sandbox via MCP tools
  or curl to configure scenarios, run them, and debug from webhooks and
  request logs. Use this skill whenever the user mentions WhatsApp usernames,
  BSUIDs, business-scoped user IDs, hidden/missing phone numbers in WhatsApp
  webhooks, wa_id/user_id/recipient_user_id fields, the username-sandbox,
  360dialog or Meta username changes, testing or debugging a WhatsApp
  integration, or any of the error codes above — even if they don't name the
  sandbox explicitly.
---

# WhatsApp usernames/BSUID: configure, test, debug

WhatsApp is rolling out **usernames**: consumers can hide their phone number
behind a handle, and businesses identify them via a **Business-Scoped User ID
(BSUID)** instead. Most existing integrations silently assume a phone number
is always present — that assumption is about to break. This skill helps the
user find out *where* theirs breaks, using the **username-sandbox**: a
stateful mock of the 360dialog WhatsApp API.

## How to work: the loop

Follow this loop for every request — whether it starts as "test my use case",
"why am I getting this error", or "what will my webhooks look like":

1. **Clarify** the scenario until it's concrete (see interview checklist).
2. **Configure** the sandbox to produce exactly that scenario.
3. **Execute** the API calls — or have the user's own integration make them.
4. **Observe** — immediately after executing, call `get_webhooks` and
   `get_request_log` yourself. Never ask the user to check manually.
5. **Explain** the results (see "Explaining results" below).

Don't skip step 1: most confusion in this domain comes from under-specified
scenarios ("user sends a message" — with a username? known phone? pre- or
post-GA?). Never skip step 4: always fetch the actual data and explain it —
claims like "the phone disappears" are verifiable, so verify them.

### Interview checklist

Ask only what the scenario leaves ambiguous (don't interrogate; 2–4 sharp
questions max, then propose defaults):

- **Platform stage**: today's behavior (pre-GA, phones always visible) or
  post-GA (phones can vanish)? Default: post-GA — that's what they're testing for.
- **The consumer**: adopted a username? already in the business's contact
  book? interacted with this number in the last 30 days?
- **Direction**: user messages the business first, or business initiates?
  (Drives the 24h window: free-form sends need a window opened by an inbound.)
- **Message kind**: free-form text, template (utility/marketing/**auth**?),
  or REQUEST_CONTACT_INFO?
- **What their code consumes**: webhooks (statuses/inbound), API responses,
  or both? Where should webhooks be delivered — their endpoint URL, or is the
  sandbox delivery log enough?

### Tools

Prefer the **MCP tools** when available (`create_api_key`, `use_api_key`,
`get_config`, `update_config`, `set_webhook_url`, `send_business_message`,
`send_user_message`, `create_template`, `get_webhooks`, `get_request_log`).
The session remembers the active key. If MCP isn't connected, use curl against
the API (default `http://localhost:8000`; OpenAPI at `/docs`) — same surface:
three sandbox endpoints (`POST /sandbox/keys`, `PUT /sandbox/webhook`,
`PUT /sandbox/config`) plus the real Meta/360dialog endpoints, with the
`D360-API-KEY` header. If both fail, the service probably isn't running:
`cd username-sandbox && .venv/bin/uvicorn app.main:app --port 8000`.

Two sandbox facts that shape every test plan:

- **One key = one virtual business number talking to ONE simulated consumer.**
  The config describes "the user I'm currently talking to". Different
  consumer states = flip the config between calls, or create a second key.
- **Webhooks are recorded even without an endpoint URL** — `get_webhooks`
  (or `GET /sandbox/webhook`) is the observation tool. Set a real URL only
  when the user wants to exercise their own receiver.

Tip: set `statuses.delays_ms = [0,0,0]` and zero action delays in test
configs so results are immediately observable; defaults simulate realistic
~2s/5s latencies.

## The rules that decide everything

The single most important rule — **phone visibility** (who sees `wa_id` /
`from` / `recipient_id`):

```
visible(consumer→business)?
  forced by config (phone_visibility always/never)? → that
  pre-GA (ga_mode=false)?                           → yes, always
  consumer has NO username?                         → yes, always
  contact book entry exists (portfolio-scoped)?     → yes
  30-day cache fresh (PER business number)?         → yes
  otherwise                                         → no (BSUID-only)
```

And its critical asymmetry: **messages the business addresses BY PHONE always
echo the phone in their statuses** (the business obviously knows it — Meta's
identifier quick reference). Visibility gates BSUID-addressed sends, inbound
messages, and contacts webhooks only.

Identifier matrix (memorize; full payload examples in
[references/payloads.md](references/payloads.md)):

| Field | When present |
|---|---|
| `user_id` / `from_user_id` / `recipient_user_id` | **always** |
| `wa_id` / `from` (inbound) | only if visible |
| `recipient_id` (statuses) | phone-addressed: always; BSUID-addressed: only if visible |
| `profile.username` (no `@`) | iff username adopted; on statuses only for delivered/read, **never on sent** |
| `parent_user_id` / `recipient_parent_user_id` | iff parent BSUIDs enabled |
| `contacts` array on **failed** status | **never** (and phone-addressed failed has no `recipient_user_id`) |

State-changing events the user must understand:

- **Contact book writes**: phone-addressed traffic, visible inbound,
  `contact_request` shares. A *delivered BSUID send* writes an entry too, but
  flagged so it never reveals the phone (otherwise one reply would defeat
  BSUID-only mode).
- **`DELETE /contact_book`** clears the entry AND the 30-day cache for all
  numbers in the portfolio → immediate flip to BSUID-only; inbound webhooks
  keep arriving (an alpha bug dropped them — the sandbox is the regression
  proof).
- **24h window** opens/refreshes on any consumer inbound (incl. shares).
  Free-form sends outside it → `131047`; templates always pass.
- **Phone change** (sandbox: send to a different phone after prior phone
  traffic): all BSUIDs regenerate, old ones → `131009`, a `system` webhook
  announces old/new `user_id`.
- **BSUID scoping**: BSUIDs are per portfolio (= per key). Another key's
  generated BSUID → `131009`. Malformed (not `XX.<18-20 digits>` or
  `XX.ENT.<digits>`) → `131009`.

## Errors: recognize → reproduce → prescribe

| Code | Meaning | Partner's correct handling |
|---|---|---|
| `131009` | malformed / foreign / retired BSUID | re-resolve identity; after a `system` phone-change webhook, replace stored BSUIDs |
| `131047` | free-form send outside 24h window | fall back to an approved template |
| `131062` | auth-flavored template → BSUID recipient | auth templates only to phone-addressed recipients |
| `131056` (HTTP 429) | username change rate limit (3 per 14 days, per number) | retry after timestamp in details — **not** "name taken" |
| `147001` | username not available (taken, case-insensitive) | pick another name |
| `100` | invalid param: bad username format, unknown config key, `contact_request` typo (use `request_contact_info`), wrong type | fix the request |
| `132001` | template name doesn't exist | create the template first |
| subcode `2388050` / `2388153` | REQUEST_CONTACT_INFO button text missing / ≠ `"Share Contact Info"` | the button text is immutable |

Every error envelope's `error_data.details` states the actual cause — read it
to the user verbatim before interpreting. To make an integration *experience*
an error deliberately, use `inject_error` in the config (e.g.
`{"on": "messages", "code": 131047, "times": 1}`) — it fires on the next N
matching calls, then auto-clears.

Full table with reproduction commands: [references/errors.md](references/errors.md).

## Debugging someone's integration

When the user reports "my integration does X wrong" or shows a payload:

1. `get_request_log` (`errors_only=true` first) — what did they *actually*
   send, and which envelope came back? Most "sandbox bugs" are a missing
   field or a phone in `recipient` / BSUID in `to`.
2. `get_webhooks` — what was delivered, and was it BSUID-only when they
   expected a phone (or vice versa)? Map the observation back to the
   visibility algorithm step by step and name the exact rule that fired.
3. If their endpoint isn't receiving: check `delivery` status and
   `endpoint_response` in the webhook log (5 retries with backoff are
   recorded), and whether a URL is configured at all.
4. Reproduce minimally with sandbox calls before concluding anything about
   their code.

## Ready-made scenario recipes

[references/recipes.md](references/recipes.md) has step-by-step recipes
(config + calls + what to expect) for the 14 canonical use cases: BSUID-only
inbound, replying to a BSUID, wamid correlation, phone precedence, window
fallback, auth-template fallback, phone recovery via REQUEST_CONTACT_INFO,
manual contact share, username claiming + rate limit, failed statuses,
pre-GA/GA flip, contact-book deletion, parent BSUIDs, and error injection.
Read it when the user's scenario matches one — adapt rather than re-derive.

Full domain rules (visibility, cache/book mechanics, username format rules,
parent BSUIDs, GA semantics): [references/rules.md](references/rules.md).
Exact payload shapes for every webhook type: [references/payloads.md](references/payloads.md).

## Explaining results

After every experiment, always produce a structured debrief — even if the
user didn't ask. Pull the actual data first (`get_webhooks`, `get_request_log`),
then explain in this order:

1. **What happened** — quote the key fields from the actual webhook/response
   verbatim. Never paraphrase. Point at what's present and what's absent.
2. **Why** — name the exact visibility rule that fired ("no `recipient_id`
   because: BSUID-addressed, GA on, username adopted, no contact-book entry,
   cache cold → BSUID-only").
3. **What your service will receive** — describe the webhook payload shape
   their handler will see in production: which fields are guaranteed, which
   are conditional, and what breaks if you assume the optional ones are there.
4. **Pass criterion** — one concrete sentence: "your handler must key
   conversations on `from_user_id` and treat `from` as optional". This is
   what "passing" this scenario means.
5. **Flag anything unexpected** — if the observed behavior doesn't match the
   rules, say so plainly. The sandbox is deterministic: a mismatch means a
   misconfiguration (`get_config` to verify) or a real finding.

## Guided readiness audit

When the user asks anything like "help me make sure my service handles this
correctly", "walk me through what I need to test", "is my integration ready
for the username rollout", or similar broad readiness questions — run the
full audit as a guided sequence, one scenario at a time.

**Do not dump all recipes at once.** Instead:

1. Briefly explain what you're about to do: "I'll walk you through 8
   scenarios that cover the critical cases. We'll go one at a time — I'll
   run each experiment, show you what the sandbox produced, and tell you
   what your service needs to handle. Ready?"
2. For each scenario in this sequence (drawn from the composite audit in
   [references/recipes.md](references/recipes.md)):
   - State the scenario name and what it tests (one sentence).
   - Configure and execute it.
   - Immediately fetch results and deliver the full debrief (steps 1–5 above).
   - State **PASS** or **NEEDS ATTENTION** based on whether their service
     would handle it correctly given what you observed.
   - Ask: "Ready for the next one?" before continuing.
3. After all scenarios, deliver a summary table:
   | Scenario | Result | Action needed |
   with one row per test. Flag any NEEDS ATTENTION items with the specific
   code change required.

**Audit sequence** (in order):
1. BSUID-only inbound — state U+, CB- (run all three state variants: U+/CB-,
   U-/CB-, U+/CB+)
2. Reply to a BSUID
3. Wamid correlation without a phone
4. Closed window → 131047 → template fallback
5. Auth template → 131062 → phone fallback
6. Phone recovery via REQUEST_CONTACT_INFO
7. Failed statuses (no `contacts` array)
8. Pre-GA ↔ GA flip

Start every audit on a fresh key so state is clean. Set zero delays in the
first `update_config` so results are immediate.
