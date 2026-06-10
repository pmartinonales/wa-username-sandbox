"""MCP server for the username-sandbox API.

Lets an agent drive the whole sandbox without the dashboard UI: create/use API
keys, flip the behavior config, send messages as the business or as the
simulated end-user, and inspect requests, responses, errors and webhook
deliveries.

Run (stdio):  SANDBOX_BASE_URL=http://localhost:8000 python mcp_server.py
Register:     claude mcp add username-sandbox \
                -e SANDBOX_BASE_URL=http://localhost:8000 \
                -- /path/to/.venv/bin/python /path/to/mcp_server.py
"""
import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("SANDBOX_BASE_URL", "http://localhost:8000")

mcp = FastMCP(
    "username-sandbox",
    instructions=(
        "Drives the username-sandbox: a stateful mock of the 360dialog WhatsApp "
        "API simulating the usernames/BSUID lifecycle (phones hidden behind "
        "handles, Business-Scoped User IDs, contact book, 24h window). Typical "
        "flow: create_api_key → update_config (choose the simulated user's "
        "state) → send_business_message / send_user_message → get_webhooks and "
        "get_request_log to see what happened. One key = one virtual business "
        "number talking to ONE simulated consumer. The active key is remembered "
        "for the session."
    ),
)

_state: dict[str, str | None] = {"key": os.environ.get("SANDBOX_API_KEY")}


async def _call(method: str, path: str, *, body: Any = None,
                needs_key: bool = True) -> str:
    headers = {}
    if needs_key:
        if not _state["key"]:
            return json.dumps({"error": "No API key set for this session. "
                                        "Call create_api_key (or use_api_key) first."})
        headers["D360-API-KEY"] = _state["key"]
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
            r = await client.request(method, path, json=body, headers=headers)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"Sandbox API unreachable at {BASE}: {e}. "
                                    "Is the service running?"})
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:2000]}
    return json.dumps({"http_status": r.status_code, "body": data}, indent=2)


# ------------------------------------------------------------------- keys

@mcp.tool()
async def create_api_key(name: str = "claude-mcp", country: str = "BR") -> str:
    """Create a new sandbox API key (a virtual WhatsApp business number with one
    simulated consumer counterpart) and make it the session's active key.
    Returns the key, business number, WABA id and expiry."""
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
            r = await client.post("/sandbox/keys", json={"name": name, "country": country})
    except httpx.HTTPError as e:
        return json.dumps({"error": f"Sandbox API unreachable at {BASE}: {e}"})
    data = r.json()
    if r.status_code == 200:
        _state["key"] = data["d360_api_key"]
    return json.dumps({"http_status": r.status_code, "body": data,
                       "note": "This key is now the session's active key."}, indent=2)


@mcp.tool()
async def use_api_key(d360_api_key: str) -> str:
    """Switch the session to an existing sandbox API key (sk_sandbox_...).
    Validates the key against the API."""
    prev = _state["key"]
    _state["key"] = d360_api_key
    result = await _call("GET", "/sandbox/config")
    if '"http_status": 200' not in result:
        _state["key"] = prev
        return json.dumps({"error": "Key rejected by the sandbox.",
                           "details": json.loads(result)})
    return json.dumps({"ok": True, "active_key": d360_api_key})


@mcp.tool()
async def current_key() -> str:
    """Show the session's active API key (or that none is set)."""
    return json.dumps({"active_key": _state["key"], "base_url": BASE})


# ------------------------------------------------------------------ config

@mcp.tool()
async def get_config() -> str:
    """Read the active behavior config: platform stage (ga_mode), the simulated
    end-user's state (username, contact book, phone visibility, service window,
    parent BSUID), consumer auto-actions, status-webhook sequence, error
    injection. "auto" means real production rules apply."""
    return await _call("GET", "/sandbox/config")


@mcp.tool()
async def update_config(patch: dict) -> str:
    """Partially update the behavior config (deep-merged; applies to the next
    API call). Shape:
    {"ga_mode": bool,
     "user": {"has_username": bool, "username": "auto"|str, "country": "BR",
              "phone_visibility": "auto"|"always"|"never",
              "in_contact_book": "auto"|true|false,
              "service_window": "auto"|"open"|"closed", "parent_bsuid": bool},
     "consumer_actions": {"reply_to_messages": bool, "reply_text": str,
              "reply_delay_ms": int, "tap_request_contact_info": bool,
              "tap_delay_ms": int, "share_contact_manually": bool},
     "statuses": {"sequence": ["sent","delivered","read"] (or ending "failed"),
              "delays_ms": [int,...], "failed_error_code": int},
     "inject_error": null | {"on": "messages"|"marketing_messages"|"username",
              "code": int, "times": int}}
    Tip: delays_ms [0,0,0] makes webhooks arrive immediately."""
    return await _call("PUT", "/sandbox/config", body=patch)


@mcp.tool()
async def set_webhook_url(url: str, secret: str = "") -> str:
    """Point the sandbox's webhooks at a real HTTP endpoint (optional — without
    one, webhooks are still recorded and visible via get_webhooks). A secret
    enables HMAC signing (X-Sandbox-Signature)."""
    body: dict = {"url": url}
    if secret:
        body["secret"] = secret
    return await _call("PUT", "/sandbox/webhook", body=body)


# --------------------------------------------------------------- messaging

@mcp.tool()
async def send_business_message(
    to_phone: str = "",
    to_bsuid: str = "",
    text: str = "Hello from the sandbox!",
    message_type: str = "text",
    template_name: str = "",
    template_language: str = "en",
) -> str:
    """Send a message AS THE BUSINESS (POST /messages). Address with to_phone
    (digits) OR to_bsuid (e.g. BR.13491208655302741918 — any well-formed value
    attaches to the simulated user; if both are given, phone wins, mirroring
    production). message_type: "text", "template" (set template_name; create it
    first with create_template) or "request_contact_info" (interactive that the
    user can tap). Returns the API response — including error envelopes like
    131047 (closed 24h window) or 131062 (auth template to BSUID) — plus a
    wamid on success; statuses then arrive as webhooks (see get_webhooks)."""
    if not to_phone and not to_bsuid:
        return json.dumps({"error": "Provide to_phone or to_bsuid."})
    body: dict = {}
    if to_phone:
        body["to"] = to_phone
    if to_bsuid:
        body["recipient"] = to_bsuid
    if message_type == "text":
        body.update({"type": "text", "text": {"body": text}})
    elif message_type == "template":
        if not template_name:
            return json.dumps({"error": "template_name is required for template sends."})
        body.update({"type": "template",
                     "template": {"name": template_name,
                                  "language": {"code": template_language}}})
    elif message_type == "request_contact_info":
        body.update({"type": "interactive",
                     "interactive": {"type": "request_contact_info",
                                     "body": {"text": text or "Please share your contact info"},
                                     "action": {"name": "request_contact_info"}}})
    else:
        return json.dumps({"error": f"Unknown message_type '{message_type}'. Use "
                                    "text, template or request_contact_info."})
    return await _call("POST", "/messages", body=body)


@mcp.tool()
async def send_user_message(
    text: str = "Hello!",
    share_contact: bool = False,
    share_origin: str = "other",
    config_patch: dict | None = None,
) -> str:
    """Send a message AS THE SIMULATED END-USER, unprompted (the user messages
    the business first). Emits the inbound webhook with the identity state the
    config describes and opens the 24h service window. config_patch (same shape
    as update_config) is applied first, so e.g. "a user WITHOUT a username who
    IS in my contact book writes in" is one call:
    config_patch={"user": {"has_username": false, "in_contact_book": true}}.
    share_contact=true sends a contact-card share instead of text
    (share_origin "other" → with vCard; "contact_request" → button tap)."""
    body: dict = {}
    if share_contact:
        body["type"] = "contacts"
        body["origin"] = share_origin
    else:
        body["text"] = text
    if config_patch:
        body["config"] = config_patch
    return await _call("POST", "/sandbox/inbound", body=body)


@mcp.tool()
async def create_template(
    name: str,
    category: str = "utility",
    language: str = "en",
    body_text: str = "Hello {{1}}",
    auth_flavor: str = "",
    with_contact_button: bool = False,
) -> str:
    """Create a message template (auto-approved). category: "utility" or
    "marketing". auth_flavor ("one-tap"/"zero-tap"/"copy-code") marks it
    authentication-flavored — sending those to a BSUID yields error 131062.
    with_contact_button adds a REQUEST_CONTACT_INFO button ("Share Contact
    Info"), which the simulated user can tap."""
    components: list = [{"type": "BODY", "text": body_text}]
    if with_contact_button:
        components.append({"type": "BUTTONS", "buttons": [
            {"type": "REQUEST_CONTACT_INFO", "text": "Share Contact Info"}]})
    body: dict = {"name": name, "language": language, "category": category,
                  "components": components}
    if auth_flavor:
        body["auth_flavor"] = auth_flavor
    return await _call("POST", "/v1/configs/templates", body=body)


# -------------------------------------------------------------- monitoring

@mcp.tool()
async def get_webhooks(limit: int = 10, field: str = "",
                       full_payloads: bool = False) -> str:
    """Inspect recent webhook deliveries for the active key (newest first):
    inbound messages, sent/delivered/read/failed statuses, contact shares,
    business_username_update, system events. Each entry has a one-line summary
    (status/type + whether the payload was BSUID-only or carried the phone);
    set full_payloads=true for complete webhook JSON. field filters (e.g.
    "messages", "business_username_update")."""
    raw = await _call("GET", "/sandbox/webhook")
    parsed = json.loads(raw)
    if parsed.get("http_status") != 200:
        return raw
    rows = parsed["body"]["deliveries"]
    if field:
        rows = [r for r in rows if r["field"] == field]
    rows = rows[:max(1, min(limit, 50))]
    out = []
    for r in rows:
        value = r["payload"]["entry"][0]["changes"][0]["value"]
        entry: dict = {"at": r["created_at"], "field": r["field"],
                       "delivery": r["status"], "attempts": r["attempts"],
                       "endpoint_response": r["response_code"]}
        if isinstance(value, dict) and value.get("statuses"):
            s = value["statuses"][0]
            entry["summary"] = (f"status={s['status']} wamid={s['id'][:28]}… "
                                f"{'phone+BSUID' if s.get('recipient_id') else 'BSUID-only'}")
            if s.get("errors"):
                entry["errors"] = s["errors"]
        elif isinstance(value, dict) and value.get("messages"):
            m = value["messages"][0]
            entry["summary"] = (f"inbound type={m['type']} "
                                f"{'phone+BSUID' if m.get('from') else 'BSUID-only'}")
        elif isinstance(value, dict) and value.get("status"):
            entry["summary"] = f"business_username_update status={value['status']}"
        if full_payloads:
            entry["payload"] = r["payload"]
        out.append(entry)
    return json.dumps({"webhook_endpoint": parsed["body"]["url"],
                       "deliveries": out}, indent=2)


@mcp.tool()
async def get_request_log(limit: int = 10, errors_only: bool = False) -> str:
    """Inspect the active key's recent API requests/responses (newest first):
    method, path, HTTP status, WhatsApp error code, duration, and the request
    and response bodies — the way to debug what an integration actually sent
    and which error envelope came back. errors_only=true filters to failures."""
    raw = await _call("GET", f"/sandbox/requests?limit={max(1, min(limit * 3, 200))}")
    parsed = json.loads(raw)
    if parsed.get("http_status") != 200:
        return raw
    rows = parsed["body"]["data"]
    if errors_only:
        rows = [r for r in rows if r["status_code"] >= 400]
    rows = rows[:max(1, min(limit, 50))]
    for r in rows:
        for key in ("request_body", "response_body"):
            try:
                r[key] = json.loads(r[key]) if r[key] else None
            except (ValueError, TypeError):
                pass
    return json.dumps({"requests": rows}, indent=2)


if __name__ == "__main__":
    mcp.run()
