# Scenario recipes

Each recipe: goal → config → calls → what to observe → pass criterion.
Calls shown as MCP tools; the curl mapping is mechanical (`update_config` =
`PUT /sandbox/config`, `send_business_message` = `POST /messages`,
`send_user_message` = `POST /sandbox/inbound`, `get_webhooks` =
`GET /sandbox/webhook`). Start every session with `create_api_key` unless the
user already has one (`use_api_key`). Add
`{"statuses": {"delays_ms": [0,0,0]}, "consumer_actions":
{"reply_delay_ms": 0, "tap_delay_ms": 0}}` to configs so observations are
immediate.

A useful default consumer state per recipe is noted as **state:**
(`U+`=has username, `U-`=no username, `CB+`=in contact book, `CB-`=not).

---

### 1. BSUID-only inbound (the headline scenario) — state: U+, CB-
- config: `{"ga_mode": true, "user": {"has_username": true, "in_contact_book": "auto", "phone_visibility": "auto"}}`
- `send_user_message(text="hi")`
- observe (`get_webhooks`): inbound with `from_user_id` + `profile.username`,
  **no `from` / `wa_id`**.
- pass: their handler keys the conversation on `from_user_id`, renders the
  username, never dereferences phone fields.
- variants: `U-` → phone present, no username field. `CB+` → phone AND
  username present. Run all three; it's one config flip each.

### 2. Reply to a BSUID
- after recipe 1, take `from_user_id` from the webhook;
  `send_business_message(to_bsuid=<it>, text=...)`.
- observe: response `{input, user_id}` (no `wa_id`) + wamid.
- pass: they read `user_id`, store the wamid for correlation.

### 3. Correlate statuses without a phone — state: U+, CB-
- `send_business_message(to_bsuid=...)`; observe statuses: `id` = wamid,
  `recipient_user_id` present, `recipient_id` absent; `username` appears on
  delivered/read only, never sent.
- pass: delivery state keyed on wamid alone.

### 4. Phone precedence
- `POST /messages` with BOTH `to` and `recipient` (raw curl or request_log
  inspection of their integration) → response has `wa_id`, no `user_id`.
- pass: they know `to` wins and don't double-address by accident.

### 5. Closed window → 131047 → template fallback
- config `{"user": {"service_window": "closed"}}`;
  `send_business_message(text=...)` → `131047`.
- `create_template(name="reengage", category="utility")`;
  resend as `message_type="template"` → succeeds.
- then `update_config({"user": {"service_window": "auto"}})` +
  `send_user_message(...)` → window reopens; free-form succeeds again.
- pass: their 131047 handler falls back to a template, not a retry loop.

### 6. Auth template → 131062 → phone fallback
- `create_template(name="otp", auth_flavor="one-tap")`;
  send by BSUID → `131062`; same template by phone → succeeds.
- pass: auth flows route by phone only.

### 7. Phone recovery via REQUEST_CONTACT_INFO — state: U+, CB-
- config: tap on — `{"consumer_actions": {"tap_request_contact_info": true, "tap_delay_ms": 0}}`
- `send_business_message(to_bsuid=..., message_type="request_contact_info")`
  (or a template with the contact button — text exactly `"Share Contact
  Info"`, see errors 2388050/2388153).
- observe: contacts webhook `origin="contact_request"`, **no vCard**, phone
  in `phones[0]`; every subsequent webhook carries the phone again.
- pass: they parse the share, update their contact record, and reconcile the
  BSUID-keyed conversation with the recovered phone.

### 8. Manual contact share (vCard variant)
- config `{"consumer_actions": {"share_contact_manually": true}}` + any
  delivered send, or directly `send_user_message(share_contact=true,
  share_origin="other")`.
- observe: `origin="other"` WITH `vcard`; visibility does NOT change.
- pass: both share variants parse; only `contact_request` updates state.

### 9. Username claim + rate limit
- `GET /username_suggestions` → claim one → `business_username_update`
  webhook (`approved` under GA, `reserved` pre-GA).
- claim 2 more, then a 4th → HTTP 429 / `131056` with next-available
  timestamp. Duplicate of another business's name → `147001`.
- pass: 429 handled as "wait", 147001 as "rename" — never confused.

### 10. Failed statuses
- config `{"statuses": {"sequence": ["sent", "failed"], "delays_ms": [0, 0], "failed_error_code": 131026}}`
- send; observe failed status: no `contacts` array, `errors[]` present,
  identifier per addressing (phone→`recipient_id` only, BSUID→`recipient_user_id` only).
- pass: failure handler tolerates the missing `contacts` and reads `errors[]`.

### 11. Pre-GA ↔ GA flip
- `{"ga_mode": false}`: BSUID send statuses still carry the phone; inbound
  carries `from`. Flip `{"ga_mode": true, "user": {"phone_visibility": "auto"}}`
  (with U+, CB-): same calls go BSUID-only.
- pass: one code path works in both worlds — no "phone is always there"
  assumptions, no "username means hidden" shortcuts either (CB+ shows both).

### 12. Contact-book deletion mid-conversation
- make the phone known (phone send), grab `recipient_user_id`;
  `DELETE /contact_book?messaging_product=whatsapp&bsuid=<it>` →
  `{success: true, deleted: true}`.
- subsequent BSUID sends: statuses BSUID-only; `send_user_message` still
  produces inbound webhooks (BSUID-only).
- pass: conversation survives the phone vanishing mid-thread.

### 13. Parent BSUIDs
- config `{"user": {"parent_bsuid": true}}`; `GET /parent-bsuid-accounts`
  shows the account; sends/statuses now include `parent_user_id` /
  `recipient_parent_user_id`; sending TO the parent value works.
- pass: `XX.ENT.` ids stored as a distinct type, never sent to a
  non-enrolled number (→ `131009`).

### 14. Error-handling drills (inject_error)
- `update_config({"inject_error": {"on": "messages", "code": 131047, "times": 1}})`
  → next send fails with that envelope, the following succeeds; config shows
  `inject_error: null` after.
- pass: retries/alerting fire once, recover cleanly, no poison-pill loops.

---

## Composite session for "test my whole integration"

When the user wants broad coverage rather than one scenario, run this
sequence on a fresh key, pointing `set_webhook_url` at THEIR endpoint so
their real handler is exercised: 1 (all three state variants) → 2 → 3 → 5 →
6 → 7 → 10 → 11 → 12. After each step, check `get_request_log
(errors_only=true)` for anything their integration sent that errored, and
summarize per the matrix. That's the full username-readiness audit in ~15
calls.
