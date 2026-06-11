# Domain rules — usernames/BSUID, in full

## Contents
1. [Identities](#identities)
2. [Phone visibility](#phone-visibility)
3. [Contact book & 30-day cache](#contact-book--30-day-cache)
4. [24-hour service window](#24-hour-service-window)
5. [Sending rules](#sending-rules)
6. [Business username rules](#business-username-rules)
7. [Parent BSUIDs](#parent-bsuids)
8. [GA mode](#ga-mode)
9. [Sandbox-specific conventions](#sandbox-specific-conventions)

## Identities

- **Phone number** (`wa_id`, `from`, `recipient_id`): clean digits, E.164
  without `+` (except `phones[0].phone` in contact shares, which carries `+`).
- **BSUID** (`user_id`, `from_user_id`, `recipient_user_id`): format
  `<COUNTRY>.<18–20 digits>`, e.g. `BR.13491208655302741918`. **Scoped per
  business portfolio** — the same consumer has a different BSUID for every
  portfolio, and one portfolio's BSUID is meaningless (→ `131009`) in another.
  Generated lazily on first interaction. Stable across username changes;
  regenerated only on consumer phone change.
- **Parent BSUID** (`parent_user_id`): `<COUNTRY>.ENT.<digits>`, shared across
  the portfolios enrolled in a parent account. Only present when enabled.
- **Username**: consumer handle, no `@` prefix in payloads. Adopting,
  changing or removing a username never changes the BSUID.
- **wamid**: message id returned by every successful send and echoed in
  `statuses[].id` — the only correlation key that always works.

## Phone visibility

Evaluated per (business number, consumer) whenever a payload would contain
the consumer's phone:

1. Sandbox override `user.phone_visibility`: `"always"` → visible, `"never"`
   → hidden. (`"auto"` falls through to the real rules.)
2. `ga_mode=false` (pre-GA) → visible. Phones never disappear before GA.
3. Consumer has no username → visible. Hiding requires adopting a handle.
4. Contact book entry for (portfolio, consumer) → visible.
   Sandbox override `user.in_contact_book`: `true` forces the entry to exist,
   `false` forces it absent, `"auto"` uses real state.
5. 30-day cache fresh for (business **number**, consumer) → visible. The
   cache is per number, NOT per portfolio: number A's interaction does not
   give number B the phone (B may still get it from the portfolio-wide
   contact book).
6. Otherwise → hidden (BSUID-only payloads).

**Asymmetry that trips everyone up:** statuses of messages the business
addressed *by phone* always include `recipient_id`/`wa_id`, regardless of
the above — the business already knows that phone; it typed it. Visibility
gates BSUID-addressed statuses, inbound messages, and contacts webhooks.

## Contact book & 30-day cache

Both are "does the business know the phone" records, at different scopes:

| | Contact book | Cache |
|---|---|---|
| Scope | portfolio | business number |
| Expiry | never (until deleted) | 30 days after last interaction |
| Grants visibility | yes | yes (while fresh) |

Written by (both at once): phone-addressed sends, inbound messages while the
phone is visible, and `contact_request` shares (the "Share Contact Info"
tap — this is *the* recovery mechanism). A **delivered BSUID-addressed send**
also writes entries, but flagged `phone_known=false`: they exist (alpha
behavior) but never grant visibility — otherwise a single delivered reply
would defeat BSUID-only mode. A manual share (`origin="other"`) does NOT
write the book/cache.

`DELETE /contact_book?messaging_product=whatsapp&bsuid=<BSUID>` removes the
portfolio's entry AND clears the cache for **all numbers in the portfolio**
(alpha-confirmed). Response `{success, deleted}`; `deleted:false` if no entry
existed. Rejects parent BSUIDs and foreign BSUIDs with `success:false`, HTTP
400. After deletion (GA + username): immediate BSUID-only, but inbound
webhooks keep arriving.

## 24-hour service window

- Opens/refreshes on every consumer inbound message — text, contact shares
  included.
- Free-form sends (`text`, `interactive`) require an open window → else
  `131047`. Template sends always pass (that's what templates are for).
- Applies identically to phone sends and BSUID sends.
- Sandbox: `user.service_window` — `"open"`/`"closed"` force it; `"auto"`
  uses real state. A fresh sandbox key starts with the window open ("the
  user just opened the conversation") — production differs: there, nothing
  is open until a real inbound.

## Sending rules

`POST /messages` accepts `to` (phone) and/or `recipient` (BSUID or parent
BSUID):

- Both present → **`to` wins** (phone send).
- Phone send response: `contacts[0] = {input, wa_id}`. BSUID send response:
  `contacts[0] = {input, user_id}`. Never both; never a BSUID inside `wa_id`.
  Every success returns a wamid.
- Supported types: `text`, `template`, `interactive` (only
  `request_contact_info`; the Meta-docs typo `contact_request` → corrective
  `100`). Anything else → `100` "not implemented in sandbox".
- Auth-flavored templates (one-tap / zero-tap / copy-code) cannot target
  BSUIDs → `131062`. Send them by phone.
- `POST /marketing_messages`: template-only, marketing-category-only,
  response adds `"message_status": "accepted"`.

## Business username rules

Format (violation → `100`): 3–35 chars of `a-z0-9._` (case-insensitive),
at least one letter, no leading/trailing/double `.`, must not start with
`www`, must not end in a domain suffix
(`.com .org .net .int .edu .gov .mil .us .in .html`).

- Uniqueness: global, case-insensitive, `.`/`_` significant. Taken → `147001`.
- **Rate limit: 3 changes per number per rolling 14 days** → HTTP 429, code
  `131056`, details include the next-available ISO timestamp. (Production
  historically returned a misleading "already claimed" here; correct
  handling distinguishes 429/131056 from 147001.)
- Claim status: `reserved` pre-GA, `approved` at GA claim time, reads as
  `active` once GA is on. `GET /username` → `{}` when none claimed.
- Every claim/delete emits a `business_username_update` webhook
  (status `reserved|approved|deleted`; `username` omitted on `deleted`).
- Suggestions: `GET /username_suggestions` → 3 available names.

## Parent BSUIDs

For businesses with multiple portfolios that need one consumer identity
across them. When enabled (`user.parent_bsuid: true` in the sandbox):

- Consumers get one `parent_user_id` (`XX.ENT.<digits>`) shared across
  enrolled portfolios; webhooks include both `user_id` and `parent_user_id`
  (`recipient_user_id` and `recipient_parent_user_id` on statuses).
- Sends may target the parent BSUID from any enrolled portfolio's number;
  the response echoes the parent value as `user_id`.
- `GET /parent-bsuid-accounts` → account id + enrolled portfolios (empty
  when disabled). Parent sends while not enrolled → `131009`.

## GA mode

- `ga_mode=false` (pre-GA, today): phones visible everywhere, username
  claims come back `reserved`, the username/BSUID machinery is dormant.
- `ga_mode=true` (post-GA): the visibility algorithm is live; claims come
  back `approved`/`active`.
- Integrations must work in BOTH; test the flip explicitly (recipe 11).

## Sandbox-specific conventions

Deliberate deviations, so the user isn't surprised:

- One simulated consumer per key; sends to any well-formed phone/BSUID
  attach to them. Tester-invented BSUID values may be reused across keys
  (BSUIDs are portfolio-scoped); sandbox-*generated* ones are key-scoped →
  `131009` cross-key.
- Window starts open at key creation; force `"closed"` to test re-engagement.
- Phone change is simulated by sending to a *different* phone after prior
  phone traffic (regenerates BSUIDs + `system` webhook).
- Cache/window aging can't be time-traveled; use the forced config states.
- Templates auto-approve. Pricing blocks in statuses are decorative.
- Keys expire after 90 days; `POST /sandbox/keys` is IP-rate-limited.
