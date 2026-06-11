# Payload reference — every webhook and response shape

All webhooks use the envelope
`{"object": "whatsapp_business_account", "entry": [{"id": "<WABA_ID>",
"changes": [{"value": {...}, "field": "<field>"}]}]}` and are POSTed with
`Content-Type: application/json` (+ `X-Sandbox-Signature: sha256=<HMAC>` when
a secret is set; 5 retries with exponential backoff on non-2xx).

Annotations below mark the conditional fields — they're the whole point.

## Send responses (`POST /messages`, `/marketing_messages`)

```json
// phone-addressed (or both to+recipient: phone wins)
{"messaging_product": "whatsapp",
 "contacts": [{"input": "<as supplied>", "wa_id": "<digits>"}],
 "messages": [{"id": "<wamid>"}]}                    // + "message_status":"accepted" on /marketing_messages

// BSUID-addressed
{"messaging_product": "whatsapp",
 "contacts": [{"input": "<as supplied>", "user_id": "<BSUID>"}],
 "messages": [{"id": "<wamid>"}]}
```

`wa_id` XOR `user_id`, never both, never a BSUID in `wa_id`. Always a wamid.

## Inbound message (field `messages`)

```json
{"messaging_product": "whatsapp",
 "metadata": {"display_phone_number": "<biz>", "phone_number_id": "<id>"},
 "contacts": [{
   "profile": {"name": "<display name>",
               "username": "<handle>"},      // iff adopted; no '@'
   "wa_id": "<phone>",                       // iff visible
   "user_id": "<BSUID>",                     // always
   "parent_user_id": "<XX.ENT...>"           // iff parent enabled
 }],
 "messages": [{
   "from": "<phone>",                        // iff visible
   "from_user_id": "<BSUID>",                // always
   "id": "<wamid>", "timestamp": "<unix>",
   "type": "text", "text": {"body": "..."}
 }]}
```

## Status update (field `messages`, `statuses` present)

```json
{"messaging_product": "whatsapp", "metadata": {...},
 "contacts": [{
   "profile": {"name": "...",
               "username": "<handle>"},      // delivered/read only, iff adopted — NEVER on sent
   "wa_id": "<phone>",                       // phone-addressed: always; BSUID-addressed: iff visible
   "user_id": "<BSUID>",
   "parent_user_id": "..."                   // iff parent enabled
 }],
 "statuses": [{
   "id": "<wamid>",                          // correlate on THIS
   "status": "sent" | "delivered" | "read",
   "timestamp": "<unix>",
   "recipient_id": "<phone>",                // phone-addressed: always; BSUID-addressed: iff visible
   "recipient_user_id": "<BSUID>",           // always
   "recipient_parent_user_id": "...",        // iff parent enabled
   "conversation": {"id": "<hash>", "origin": {"type": "service|utility|marketing"}},
   "pricing": {"billable": true, "pricing_model": "PMP", "category": "<same>", "type": "regular"}
 }]}
```

## Failed status

```json
{"messaging_product": "whatsapp", "metadata": {...},
 // NO "contacts" array — handlers must not assume it
 "statuses": [{
   "id": "<wamid>", "status": "failed", "timestamp": "<unix>",
   "recipient_id": "<phone>",                // phone-addressed ONLY (then no recipient_user_id)
   "recipient_user_id": "<BSUID>",           // BSUID-addressed ONLY
   "errors": [{"code": 131049, "title": "...",
               "error_data": {"details": "..."}}]
 }]}
```

## Contacts webhook (contact share; field `messages`)

```json
{"messaging_product": "whatsapp", "metadata": {...},
 "contacts": [ ...same contact block as inbound... ],
 "messages": [{
   "id": "<wamid>", "timestamp": "<unix>", "type": "contacts",
   "from": "<phone>",                        // iff visible at emission time
   "from_user_id": "<BSUID>",
   "contacts": [{
     "name": {"formatted_name": "...", "first_name": "..."},
     "phones": [{"phone": "+<E.164>",        // the ONE field with '+'
                 "wa_id": "<digits>", "type": "MOBILE"}],
     "origin": "contact_request" | "other",
     "vcard": "BEGIN:VCARD..."               // ONLY when origin="other"
   }]
 }]}
```

`origin="contact_request"` (Share-Contact-Info tap): writes contact book +
cache → the phone is visible in everything afterwards. `origin="other"`
(manual share): carries the vCard but does NOT change visibility state.

## business_username_update (own field)

```json
{"value": {"display_phone_number": "<biz>",
           "username": "<name>",             // omitted when status="deleted"
           "status": "reserved" | "approved" | "deleted"},
 "field": "business_username_update"}
```

## System webhook (consumer phone change; field `messages`)

```json
{"messaging_product": "whatsapp", "metadata": {...},
 "messages": [{
   "id": "<wamid>", "timestamp": "<unix>", "type": "system",
   "system": {"body": "User changed phone number",
              "user_id": "<old BSUID>",
              "new_user_id": "<new BSUID>"}
 }]}
```

On receipt, integrations must rewrite their stored BSUID mapping — the old
one returns `131009` forever after.

## Observation endpoints (sandbox)

- `get_webhooks` MCP tool / `GET /sandbox/webhook`: config + last 50
  deliveries `{field, payload, status: delivered|failed|logged, attempts,
  response_code, created_at}` — `logged` = recorded, no endpoint configured.
- `get_request_log` MCP tool / `GET /sandbox/requests`: recent API calls
  `{method, path, status_code, error_code, request_body, response_body,
  duration_ms}` — what the integration actually sent, and the envelope it got.
