"""End-to-end smoke test of the MCP server over stdio against a live API.

Usage: .venv/bin/python scripts/verify_mcp.py  (API must be running on :8000)
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def call(session: ClientSession, tool: str, args: dict | None = None) -> dict | list | str:
    result = await session.call_tool(tool, args or {})
    text = result.content[0].text
    try:
        return json.loads(text)
    except ValueError:
        return text


def check(label: str, cond: bool, detail=""):
    print(f"  {'✓' if cond else '✗'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


async def main():
    params = StdioServerParameters(
        command=str(ROOT / ".venv/bin/python"),
        args=[str(ROOT / "mcp_server.py")],
        env={"SANDBOX_BASE_URL": "http://localhost:8000"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print(f"tools: {', '.join(tools)}")
            check("11 tools registered", len(tools) == 11, tools)

            r = await call(s, "get_config")
            check("no-key guard message", "No API key set" in json.dumps(r))

            r = await call(s, "create_api_key", {"name": "mcp-verify"})
            check("create_api_key", r["http_status"] == 200, r)

            r = await call(s, "update_config", {"patch": {
                "user": {"phone_visibility": "never"},
                "statuses": {"delays_ms": [0, 0, 0]},
                "consumer_actions": {"reply_delay_ms": 0, "tap_delay_ms": 0}}})
            check("update_config", r["http_status"] == 200
                  and r["body"]["user"]["phone_visibility"] == "never", r)

            r = await call(s, "send_business_message", {
                "to_bsuid": "BR.13491208655302741918", "text": "hello user"})
            check("send_business_message (BSUID)", r["http_status"] == 200
                  and r["body"]["contacts"][0]["user_id"], r)
            await asyncio.sleep(0.5)  # let the BSUID-only statuses land first

            r = await call(s, "send_user_message", {
                "text": "hi, no handle",
                "config_patch": {"user": {"has_username": False,
                                          "phone_visibility": "auto"}}})
            check("send_user_message (no username)", r["http_status"] == 200, r)

            r = await call(s, "send_business_message", {
                "to_bsuid": "BR.13491208655302741918", "message_type": "template",
                "template_name": "nope"})
            check("template error surfaces (132001)",
                  r["body"]["error"]["code"] == 132001, r)

            r = await call(s, "create_template", {
                "name": "otp", "auth_flavor": "one-tap"})
            check("create_template", r["http_status"] == 200, r)
            r = await call(s, "send_business_message", {
                "to_bsuid": "BR.13491208655302741918", "message_type": "template",
                "template_name": "otp"})
            check("auth template to BSUID → 131062",
                  r["body"]["error"]["code"] == 131062, r)

            await asyncio.sleep(1)
            r = await call(s, "get_webhooks", {"limit": 20})
            summaries = [d.get("summary", "") for d in r["deliveries"]]
            check("webhooks: statuses visible",
                  any("status=read" in x and "BSUID-only" in x for x in summaries), summaries)
            check("webhooks: inbound visible",
                  any("inbound" in x and "phone" in x for x in summaries), summaries)

            r = await call(s, "get_request_log", {"errors_only": True})
            codes = [x["error_code"] for x in r["requests"]]
            check("request log: error codes present",
                  131062 in codes and 132001 in codes, codes)

            print("\nall MCP checks passed")


asyncio.run(main())
