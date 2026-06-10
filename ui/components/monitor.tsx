"use client";

import { useCallback, useEffect, useState } from "react";
import { Activity, ChevronDown, ChevronRight, Globe, Webhook } from "lucide-react";
import { api, type KeyInfo, type RequestRow, type WebhookRow } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

function ts(iso: string): string {
  return new Date(iso.endsWith("Z") ? iso : iso + "Z").toLocaleTimeString();
}

function pretty(raw: string | null | unknown): string {
  if (raw == null) return "—";
  if (typeof raw !== "string") return JSON.stringify(raw, null, 2);
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function StatusBadge({ code }: { code: number }) {
  const variant = code < 300 ? "success" : code < 500 ? "destructive" : "warning";
  return <Badge variant={variant}>{code}</Badge>;
}

function ExpandableRow({
  head,
  body,
}: {
  head: React.ReactNode;
  body: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b last:border-b-0">
      <button
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent/50"
        onClick={() => setOpen(!open)}
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
        {head}
      </button>
      {open && <div className="space-y-2 bg-muted/30 px-9 pb-3 pt-1">{body}</div>}
    </div>
  );
}

function RequestsList({ rows }: { rows: RequestRow[] }) {
  if (!rows.length)
    return <p className="px-3 py-8 text-center text-sm text-muted-foreground">No requests yet — call the API with this key.</p>;
  return (
    <div>
      {rows.map((r) => (
        <ExpandableRow
          key={r.id}
          head={
            <>
              <span className="w-16 font-mono text-xs font-semibold">{r.method}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-xs">{r.path}</span>
              {r.error_code != null && <Badge variant="outline">#{r.error_code}</Badge>}
              <StatusBadge code={r.status_code} />
              <span className="w-14 text-right text-xs tabular-nums text-muted-foreground">
                {r.duration_ms}ms
              </span>
              <span className="w-20 text-right text-xs tabular-nums text-muted-foreground">
                {ts(r.created_at)}
              </span>
            </>
          }
          body={
            <>
              <div>
                <p className="mb-1 text-xs font-semibold text-muted-foreground">Request</p>
                <pre className="max-h-48 overflow-auto rounded border bg-background p-2 text-xs">
                  {pretty(r.request_body)}
                </pre>
              </div>
              <div>
                <p className="mb-1 text-xs font-semibold text-muted-foreground">Response</p>
                <pre className="max-h-48 overflow-auto rounded border bg-background p-2 text-xs">
                  {pretty(r.response_body)}
                </pre>
              </div>
            </>
          }
        />
      ))}
    </div>
  );
}

function WebhooksList({ rows }: { rows: WebhookRow[] }) {
  if (!rows.length)
    return <p className="px-3 py-8 text-center text-sm text-muted-foreground">No webhooks yet — send a message to generate some.</p>;
  return (
    <div>
      {rows.map((r) => (
        <ExpandableRow
          key={r.id}
          head={
            <>
              <Badge variant="secondary" className="font-mono text-[10px]">
                {r.field}
              </Badge>
              <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                {summarize(r)}
              </span>
              <Badge
                variant={
                  r.status === "delivered" ? "success" : r.status === "failed" ? "destructive" : "secondary"
                }
              >
                {r.status}
                {r.response_code ? ` ${r.response_code}` : ""}
              </Badge>
              {r.attempts > 1 && (
                <span className="text-xs text-muted-foreground">{r.attempts}×</span>
              )}
              <span className="w-20 text-right text-xs tabular-nums text-muted-foreground">
                {ts(r.created_at)}
              </span>
            </>
          }
          body={
            <pre className="max-h-64 overflow-auto rounded border bg-background p-2 text-xs">
              {pretty(r.payload)}
            </pre>
          }
        />
      ))}
    </div>
  );
}

function summarize(r: WebhookRow): string {
  try {
    const value = (r.payload as { entry: { changes: { value: Record<string, unknown> }[] }[] })
      .entry[0].changes[0].value;
    if (Array.isArray(value.statuses)) {
      const s = value.statuses[0] as Record<string, unknown>;
      return `status: ${s.status} · ${s.recipient_id ? "phone" : "BSUID-only"}`;
    }
    if (Array.isArray(value.messages)) {
      const m = value.messages[0] as Record<string, unknown>;
      return `inbound: ${m.type} · ${m.from ? "phone" : "BSUID-only"}`;
    }
    if (value.status) return `username: ${value.status}`;
  } catch {
    /* envelope-less payloads */
  }
  return "";
}

export function Monitor({ base, keyInfo }: { base: string; keyInfo: KeyInfo }) {
  const [requests, setRequests] = useState<RequestRow[]>([]);
  const [webhooks, setWebhooks] = useState<WebhookRow[]>([]);
  const [webhookUrl, setWebhookUrl] = useState("");
  const [savedUrl, setSavedUrl] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [reqs, whs] = await Promise.all([
        api<{ data: RequestRow[] }>(base, "/sandbox/requests?limit=100", {
          key: keyInfo.d360_api_key,
        }),
        api<{ url: string | null; deliveries: WebhookRow[] }>(base, "/sandbox/webhook", {
          key: keyInfo.d360_api_key,
        }),
      ]);
      setRequests(reqs.data);
      setWebhooks(whs.deliveries);
      setSavedUrl(whs.url);
    } catch {
      /* server briefly unreachable — keep last data */
    }
  }, [base, keyInfo.d360_api_key]);

  useEffect(() => {
    refresh();
    if (paused) return;
    const id = setInterval(refresh, 2500);
    return () => clearInterval(id);
  }, [refresh, paused]);

  async function saveWebhook() {
    await api(base, "/sandbox/webhook", {
      method: "PUT",
      body: { url: webhookUrl },
      key: keyInfo.d360_api_key,
    });
    setWebhookUrl("");
    refresh();
  }

  return (
    <Card className="flex min-h-0 flex-1 flex-col">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-lg">
            <Activity className="h-5 w-5" /> Monitor
          </CardTitle>
          <Button variant="outline" size="sm" onClick={() => setPaused(!paused)}>
            {paused ? "Resume" : "Pause"}
          </Button>
        </div>
        <CardDescription>
          Live feed of API requests and webhook deliveries for this key (polls every 2.5s).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col">
        <Tabs defaultValue="requests" className="flex min-h-0 flex-1 flex-col">
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="requests" className="gap-2">
              <Globe className="h-4 w-4" /> Requests ({requests.length})
            </TabsTrigger>
            <TabsTrigger value="webhooks" className="gap-2">
              <Webhook className="h-4 w-4" /> Webhooks ({webhooks.length})
            </TabsTrigger>
          </TabsList>
          <TabsContent value="requests" className="min-h-0 flex-1 overflow-auto rounded-md border">
            <RequestsList rows={requests} />
          </TabsContent>
          <TabsContent value="webhooks" className="min-h-0 flex-1 space-y-2">
            <div className="flex items-end gap-2 px-0.5">
              <div className="flex-1 space-y-1">
                <Label className="text-xs text-muted-foreground">
                  Webhook endpoint{" "}
                  {savedUrl ? (
                    <code className="text-foreground">{savedUrl}</code>
                  ) : (
                    "(none — deliveries are logged here anyway)"
                  )}
                </Label>
                <Input
                  className="h-8"
                  placeholder="https://your-endpoint.example.com/webhooks"
                  value={webhookUrl}
                  onChange={(e) => setWebhookUrl(e.target.value)}
                />
              </div>
              <Button size="sm" variant="outline" onClick={saveWebhook} disabled={!webhookUrl.trim()}>
                Set URL
              </Button>
            </div>
            <div className="overflow-auto rounded-md border">
              <WebhooksList rows={webhooks} />
            </div>
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}
