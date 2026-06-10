"use client";

import { useState } from "react";
import { Send } from "lucide-react";
import { ApiError, api, type KeyInfo } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const EXAMPLE_BSUID = "BR.13491208655302741918";

export function SendPanel({ base, keyInfo }: { base: string; keyInfo: KeyInfo }) {
  const [addrType, setAddrType] = useState<"phone" | "bsuid">("bsuid");
  const [addr, setAddr] = useState(EXAMPLE_BSUID);
  const [msgType, setMsgType] = useState<"text" | "request_contact_info">("text");
  const [text, setText] = useState("Hello from the dashboard!");
  const [result, setResult] = useState<{ ok: boolean; body: unknown } | null>(null);
  const [busy, setBusy] = useState(false);

  function switchAddr(v: "phone" | "bsuid") {
    setAddrType(v);
    setAddr(v === "phone" ? "5511988880001" : EXAMPLE_BSUID);
  }

  async function send() {
    setBusy(true);
    setResult(null);
    const body: Record<string, unknown> =
      msgType === "text"
        ? { type: "text", text: { body: text } }
        : {
            type: "interactive",
            interactive: {
              type: "request_contact_info",
              body: { text: text || "Please share your contact info" },
              action: { name: "request_contact_info" },
            },
          };
    body[addrType === "phone" ? "to" : "recipient"] = addr;
    try {
      const r = await api(base, "/messages", {
        method: "POST",
        body,
        key: keyInfo.d360_api_key,
      });
      setResult({ ok: true, body: r });
    } catch (e) {
      setResult({ ok: false, body: e instanceof ApiError ? e.body : String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <Send className="h-5 w-5" /> Send a test message
        </CardTitle>
        <CardDescription>
          <code>POST /messages</code> — generates traffic for the monitor.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex gap-2">
          <Select value={addrType} onValueChange={(v) => switchAddr(v as never)}>
            <SelectTrigger className="h-9 w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="bsuid">BSUID</SelectItem>
              <SelectItem value="phone">Phone</SelectItem>
            </SelectContent>
          </Select>
          <Input className="h-9 font-mono text-xs" value={addr} onChange={(e) => setAddr(e.target.value)} />
        </div>
        <div className="flex gap-2">
          <Select value={msgType} onValueChange={(v) => setMsgType(v as never)}>
            <SelectTrigger className="h-9 w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="text">text</SelectItem>
              <SelectItem value="request_contact_info">contact info</SelectItem>
            </SelectContent>
          </Select>
          <Input className="h-9" value={text} onChange={(e) => setText(e.target.value)} />
          <Button className="h-9" onClick={send} disabled={busy || !addr.trim()}>
            Send
          </Button>
        </div>
        {result && (
          <pre
            className={`max-h-40 overflow-auto rounded-md border p-2 text-xs ${
              result.ok ? "bg-muted" : "border-destructive/50 bg-destructive/5"
            }`}
          >
            {JSON.stringify(result.body, null, 2)}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}
