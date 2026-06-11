# Error reference — cause, reproduction, prescription

All API errors use the Meta envelope:

```json
{"error": {"message": "(#<code>) <title>", "type": "OAuthException",
           "code": <code>,
           "error_data": {"messaging_product": "whatsapp",
                          "details": "<the actual cause, always specific>"},
           "fbtrace_id": "..."}}
```

`error_data.details` always states the real cause — quote it to the user
first. Webhook-level failures appear instead as a `failed` **status** with
`statuses[0].errors[]` (and no `contacts` array).

For each error below: what triggers it, how to reproduce it in the sandbox
(MCP tool calls; curl equivalents are 1:1), and what the partner's
integration should do.

---

## 131009 — Parameter value is not valid

**Triggers**
1. Malformed BSUID: not `XX.<18-20 digits>` (or `XX.ENT.<digits>` for parent).
   Lowercase country, missing dot, wrong digit count, letters in digits — all
   malformed.
2. Foreign BSUID: a BSUID the sandbox generated for another key/portfolio.
3. Retired BSUID: the consumer changed phone; old BSUIDs are dead.
4. Parent BSUID used while the portfolio isn't enrolled
   (`user.parent_bsuid` false).

**Reproduce**
- `send_business_message(to_bsuid="BR.123")` → malformed.
- key A: send by phone, read `recipient_user_id` from `get_webhooks`; key B:
  `send_business_message(to_bsuid=<that value>)` → foreign.
- send to phone P1, then phone P2 (triggers simulated phone change), then
  `send_business_message(to_bsuid=<old BSUID>)` → retired.

**Prescribe** Re-resolve identity instead of retrying. Specifically: on a
`system` webhook (`User changed phone number`), replace the stored
`user_id` with `new_user_id` everywhere. Never persist BSUIDs across
portfolios.

---

## 131047 — Re-engagement message (24h window closed)

**Triggers** Free-form send (`text` / `interactive`) with no consumer inbound
in the last 24h. Templates never trigger it.

**Reproduce** `update_config({"user": {"service_window": "closed"}})` then
`send_business_message(to_bsuid=..., text=...)`.

**Prescribe** Catch `131047` → resend as an approved template (utility
category for service continuations). Then verify: `create_template(...)`,
send `message_type="template"` → succeeds with the window still closed.
The window reopens on the user's next inbound (`send_user_message` shows
this).

---

## 131062 — BSUID recipients not supported (auth templates)

**Triggers** Template with `auth_flavor` (one-tap / zero-tap / copy-code)
addressed by `recipient` (BSUID). Phone-addressed auth templates are fine.

**Reproduce** `create_template(name="otp", auth_flavor="one-tap")` →
`send_business_message(to_bsuid=..., message_type="template",
template_name="otp")`.

**Prescribe** Route authentication flows through phone-addressed sends only.
If the integration only has a BSUID for the user, it cannot send them auth
templates — it must first recover the phone (REQUEST_CONTACT_INFO flow,
recipe 7) or use a different channel.

---

## 131056 (HTTP 429) — Username change rate limit

**Triggers** 4th business-username change for a number within a rolling 14
days. Details contain `Next change available at <ISO timestamp>`.

**Reproduce** Claim 3 usernames in a row (`POST /username`), then a 4th.

**Prescribe** Distinguish it from `147001`! 429/131056 = wait until the
timestamp; the name may be perfectly available. Production historically
returned a misleading "already claimed" here — integrations that retry with
a *different name* burn through the limit even faster.

---

## 147001 — Username not available

**Triggers** Claiming a username already held by another business
(case-insensitive; `.` and `_` are significant: `a.b` ≠ `a_b`).

**Reproduce** Claim `x` on key A, claim `X` on key B.

**Prescribe** Offer the user `GET /username_suggestions` alternatives.
Don't retry the same name on a schedule.

---

## 100 — Invalid parameter (the catch-all with specific details)

**Triggers**
- Username format violations (see rules.md §6 — length, charset, dots,
  `www`, domain suffixes).
- Unknown/invalid sandbox config keys or enum values.
- `interactive.type: "contact_request"` — a known Meta documentation typo;
  details reply: use `request_contact_info`.
- Unsupported message types (`image`, `video`, ... → "not implemented in
  sandbox"), missing required fields (`to`/`recipient`, `template.name`,
  webhook `url`), bad template category.

**Prescribe** Read the details; each names the offending field.

---

## 132001 — Template name does not exist (HTTP 404)

**Triggers** `type=template` send referencing a name (or name+language) not
created for this number's portfolio.

**Prescribe** `create_template` first; template names are scoped per
portfolio — another key's templates are invisible.

---

## Template button subcodes (HTTP 400, code 100 + `error_subcode`)

- **2388050** — REQUEST_CONTACT_INFO button has no `text`.
- **2388153** — button text differs from exactly `"Share Contact Info"`
  (`error_user_title: "Button text modification not allowed"`).

**Reproduce** `POST /v1/configs/templates` with a BUTTONS component and a
REQUEST_CONTACT_INFO button with text `"Share My Info"`.

**Prescribe** The button text is platform-fixed; localize the body, not the
button.

---

## Failed delivery statuses (webhook-level, not HTTP)

Configured via `statuses.sequence` ending in `"failed"` +
`statuses.failed_error_code` (e.g. 131026 "message undeliverable", default
131049 "healthy-ecosystem"). Shape rules: **no `contacts` array**;
phone-addressed → `recipient_id` and NO `recipient_user_id`;
BSUID-addressed → `recipient_user_id` and no `recipient_id`;
`statuses[0].errors[]` carries code/title/details.

**Prescribe** Failure handlers must not dereference `contacts` and must
correlate strictly by `statuses[0].id` (the wamid).

---

## Sandbox-injected errors (testing resilience)

`update_config({"inject_error": {"on": "messages", "code": <any>, "times": N}})`
fails the next N calls to that endpoint (`messages`, `marketing_messages`,
`username`) with a standard envelope, then auto-clears (visible via
`get_config` → `inject_error: null`). Use it to exercise the partner's retry
and alerting paths with *any* code, including ones the sandbox can't
naturally produce.

---

## 401 — key problems (sandbox)

Missing/unknown `D360-API-KEY`, or the key expired (90-day TTL). Details say
which; fix by `create_api_key` / `use_api_key`. If *every* call fails with
connection errors instead, the sandbox service itself is down.
