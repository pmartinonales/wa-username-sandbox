"use client";

import { useState } from "react";
import { MessageCircle } from "lucide-react";
import { ApiError, api, type KeyInfo } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

export function InboundPanel({ base, keyInfo }: { base: string; keyInfo: KeyInfo }) {
  const [text, setText] = useState("Hi! I need help with my order.");
  const [kind, setKind] = useState<"text" | "contacts-other" | "contacts-request">("text");
  const [hasUsername, setHasUsername] = useState(true);
  const [inContactBook, setInContactBook] = useState<"auto" | "true" | "false">("auto");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function trigger() {
    setBusy(true);
    setResult(null);
    const body: Record<string, unknown> = {
      // the identity state the user writes in with — persisted to the config
      config: {
        user: {
          has_username: hasUsername,
          in_contact_book: inContactBook === "auto" ? "auto" : inContactBook === "true",
        },
      },
    };
    if (kind === "text") {
      body.text = text;
    } else {
      body.type = "contacts";
      body.origin = kind === "contacts-other" ? "other" : "contact_request";
    }
    try {
      const r = await api<{ wamid: string }>(base, "/sandbox/inbound", {
        method: "POST",
        body,
        key: keyInfo.d360_api_key,
      });
      setResult({ ok: true, text: `Inbound webhook emitted (${r.wamid.slice(0, 24)}…) — see the monitor.` });
    } catch (e) {
      const detail =
        e instanceof ApiError
          ? ((e.body as { error?: { error_data?: { details?: string } } })?.error?.error_data
              ?.details ?? `HTTP ${e.status}`)
          : String(e);
      setResult({ ok: false, text: detail });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <MessageCircle className="h-5 w-5" /> Simulate inbound
        </CardTitle>
        <CardDescription>
          The user messages <em>you</em>, unprompted — with the identity state you pick here.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium">User has a username</p>
            <p className="text-xs text-muted-foreground">off → phone always visible</p>
          </div>
          <Switch checked={hasUsername} onCheckedChange={setHasUsername} />
        </div>
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium">In your contact book</p>
            <p className="text-xs text-muted-foreground">yes → phone visible despite username</p>
          </div>
          <Select value={inContactBook} onValueChange={(v) => setInContactBook(v as never)}>
            <SelectTrigger className="h-8 w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">auto</SelectItem>
              <SelectItem value="true">yes</SelectItem>
              <SelectItem value="false">no</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex gap-2">
          <Select value={kind} onValueChange={(v) => setKind(v as never)}>
            <SelectTrigger className="h-9 w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="text">text message</SelectItem>
              <SelectItem value="contacts-other">contact share (vCard)</SelectItem>
              <SelectItem value="contacts-request">contact-info tap</SelectItem>
            </SelectContent>
          </Select>
          {kind === "text" && (
            <Input className="h-9" value={text} onChange={(e) => setText(e.target.value)} />
          )}
          <Button className="h-9" onClick={trigger} disabled={busy}>
            Receive
          </Button>
        </div>
        {result && (
          <p className={`text-sm ${result.ok ? "text-emerald-600" : "text-destructive"}`}>
            {result.text}
          </p>
        )}
        <p className="text-xs text-muted-foreground">
          Also opens the 24h service window. The toggles persist to the behavior config below.
        </p>
      </CardContent>
    </Card>
  );
}
