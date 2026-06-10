# Webhook reference

Every webhook is POSTed as JSON to the URL you set with `PUT /sandbox/webhook`,
wrapped in the standard envelope:

```json
{
  "object": "whatsapp_business_account",
  "entry": [{
    "id": "<WABA_ID>",
    "changes": [{ "value": { ... }, "field": "messages" }]
  }]
}
```

Delivery mechanics: `Content-Type: application/json`; when a secret is set,
`X-Sandbox-Signature: sha256=<HMAC-SHA256 hex of the raw body>`. Non-2xx or
timeout → up to 5 retries with exponential backoff. Every attempt (and every
webhook produced while no URL is configured) is visible via
`GET /sandbox/webhook`.

---

## Identifier-inclusion matrix

The single most important thing your integration must survive: **the phone
number can be absent.** The BSUID (`user_id` / `from_user_id` /
`recipient_user_id`) is **always** present.

Inbound messages:

| Field | Username adopted | No username |
|---|---|---|
| `wa_id` / `from` | only if phone visible | always |
| `user_id` / `from_user_id` | always | always |
| `profile.username` | always (no `@`) | never |
| `parent_user_id` | if parent BSUIDs enabled | if parent BSUIDs enabled |

Statuses (`sent`/`delivered`/`read`):

| Field | Addressed by phone | Addressed by BSUID |
|---|---|---|
| `wa_id`, `recipient_id` | always | only if phone visible |
| `user_id`, `recipient_user_id` | always | always |
| `profile.username` | delivered/read only, if adopted (never on `sent`) | same |

"Phone visible" (post-GA, username adopted): contact book entry exists, OR the
30-day per-number cache is fresh, OR forced via `user.phone_visibility` /
`user.in_contact_book` in the sandbox config.

---

## 1. Inbound message (`messages`)

Sent when the simulated user messages you (`consumer_actions.reply_to_messages`).
This example is BSUID-only — username adopted, phone not visible:

```json
{
  "messaging_product": "whatsapp",
  "metadata": { "display_phone_number": "5511900000042", "phone_number_id": "200000000000042" },
  "contacts": [{
    "profile": { "name": "Sandbox User 42", "username": "user.82731942" },
    "user_id": "BR.13491208655302741918"
  }],
  "messages": [{
    "type": "text",
    "text": { "body": "Hello back!" },
    "id": "wamid.HBg...",
    "timestamp": "1780000000",
    "from_user_id": "BR.13491208655302741918"
  }]
}
```

- `wa_id` / `from` appear **only** when the phone is visible.
- `username` has no `@` prefix and appears iff the user adopted one.
- Inbound messages open/refresh the 24-hour service window.

## 2. Status updates (`messages` / `statuses`)

After each send, per your `statuses.sequence` config (default
`sent` → `delivered` ~2s → `read` ~5s). BSUID-addressed example:

```json
{
  "messaging_product": "whatsapp",
  "metadata": { "display_phone_number": "5511900000042", "phone_number_id": "200000000000042" },
  "contacts": [{
    "profile": { "name": "Sandbox User 42", "username": "user.82731942" },
    "user_id": "BR.13491208655302741918"
  }],
  "statuses": [{
    "id": "wamid.HBg...",
    "status": "delivered",
    "timestamp": "1780000002",
    "recipient_user_id": "BR.13491208655302741918",
    "conversation": { "id": "9f2b...", "origin": { "type": "service" } },
    "pricing": { "billable": true, "pricing_model": "PMP", "category": "service", "type": "regular" }
  }]
}
```

- Correlate by `statuses[0].id` (the wamid from your send response) — do not
  rely on `recipient_id`; it is omitted when the phone is not visible.
- `username` never appears on `sent`, only `delivered`/`read`.

## 3. Failed status

```json
{
  "messaging_product": "whatsapp",
  "metadata": { "display_phone_number": "5511900000042", "phone_number_id": "200000000000042" },
  "statuses": [{
    "id": "wamid.HBg...",
    "status": "failed",
    "timestamp": "1780000002",
    "recipient_user_id": "BR.13491208655302741918",
    "errors": [{
      "code": 131049,
      "title": "This message was not delivered to maintain healthy ecosystem engagement",
      "error_data": { "details": "Sandbox-simulated delivery failure." }
    }]
  }]
}
```

- **No `contacts` array** on failed statuses.
- Phone-addressed messages carry `recipient_id` and **no `recipient_user_id`**;
  BSUID-addressed the reverse.

## 4. Contacts webhook (contact share)

Sent when the user taps **Share Contact Info**
(`consumer_actions.tap_request_contact_info`, `origin: "contact_request"`) or
shares their card manually (`consumer_actions.share_contact_manually`,
`origin: "other"`):

```json
{
  "messaging_product": "whatsapp",
  "metadata": { "display_phone_number": "5511900000042", "phone_number_id": "200000000000042" },
  "contacts": [{
    "profile": { "name": "Sandbox User 42", "username": "user.82731942" },
    "user_id": "BR.13491208655302741918"
  }],
  "messages": [{
    "id": "wamid.HBg...",
    "timestamp": "1780000010",
    "type": "contacts",
    "from_user_id": "BR.13491208655302741918",
    "contacts": [{
      "name": { "formatted_name": "Sandbox User 42", "first_name": "Sandbox" },
      "phones": [{ "phone": "+5511812345678", "wa_id": "5511812345678", "type": "MOBILE" }],
      "origin": "contact_request"
    }]
  }]
}
```

- `vcard` is included **only** when `origin: "other"` (manual share); omitted
  for `contact_request`.
- All phone fields are clean digits, except `phones[0].phone` which carries `+`.
- A `contact_request` share writes the contact book + cache: the phone is
  visible in all subsequent webhooks.

## 5. `business_username_update`

Own field name; emitted on username claim/change/delete:

```json
{
  "object": "whatsapp_business_account",
  "entry": [{
    "id": "<WABA_ID>",
    "changes": [{
      "value": {
        "display_phone_number": "5511900000042",
        "username": "acme.support",
        "status": "approved"
      },
      "field": "business_username_update"
    }]
  }]
}
```

- `status`: `reserved` (pre-GA claim), `approved` (GA claim), `deleted`.
- `username` is omitted when `status` is `deleted`.

## 6. System webhook (phone change)

Sending to a different phone number after prior phone traffic simulates the
user changing their phone (v1 §7.1): all BSUIDs regenerate (old ones return
`131009`) and each affected number receives:

```json
{
  "messaging_product": "whatsapp",
  "metadata": { "display_phone_number": "5511900000042", "phone_number_id": "200000000000042" },
  "messages": [{
    "id": "wamid.HBg...",
    "timestamp": "1780000020",
    "type": "system",
    "system": {
      "body": "User changed phone number",
      "user_id": "BR.13491208655302741918",
      "new_user_id": "BR.20571208655302741001"
    }
  }]
}
```
