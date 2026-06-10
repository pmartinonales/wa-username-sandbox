"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Save, Settings2 } from "lucide-react";
import { api, errorDetails, type SandboxConfig } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";

const SEQUENCE_PRESETS: Record<string, string[]> = {
  "sent → delivered → read": ["sent", "delivered", "read"],
  "sent → delivered": ["sent", "delivered"],
  "sent only": ["sent"],
  "sent → failed": ["sent", "failed"],
};

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0">
        <p className="text-sm font-medium">{label}</p>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{children}</p>
  );
}

export function ConfigPanel({ base, apiKey }: { base: string; apiKey: string }) {
  const [cfg, setCfg] = useState<SandboxConfig | null>(null);
  const [injectOn, setInjectOn] = useState(false);
  const [status, setStatus] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const c = await api<SandboxConfig>(base, "/sandbox/config", { key: apiKey });
      setCfg(c);
      setInjectOn(c.inject_error != null);
      setStatus(null);
    } catch (e) {
      setStatus({ kind: "err", text: errorDetails(e) });
    }
  }, [base, apiKey]);

  useEffect(() => {
    load();
  }, [load]);

  function patch(fn: (c: SandboxConfig) => void) {
    setCfg((prev) => {
      if (!prev) return prev;
      const next = structuredClone(prev);
      fn(next);
      return next;
    });
  }

  async function save() {
    if (!cfg) return;
    setBusy(true);
    setStatus(null);
    try {
      const body: SandboxConfig = {
        ...cfg,
        inject_error: injectOn
          ? cfg.inject_error ?? { on: "messages", code: 131047, times: 1 }
          : null,
      };
      const saved = await api<SandboxConfig>(base, "/sandbox/config", {
        method: "PUT",
        body,
        key: apiKey,
      });
      setCfg(saved);
      setInjectOn(saved.inject_error != null);
      setStatus({ kind: "ok", text: "Saved — applies to the next API call." });
    } catch (e) {
      setStatus({ kind: "err", text: errorDetails(e) });
    } finally {
      setBusy(false);
    }
  }

  if (!cfg) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-lg">
            <Settings2 className="h-5 w-5" /> Behavior config
          </CardTitle>
          <CardDescription>
            {status?.kind === "err" ? status.text : "Loading config…"}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const seqLabel =
    Object.entries(SEQUENCE_PRESETS).find(
      ([, v]) => JSON.stringify(v) === JSON.stringify(cfg.statuses.sequence)
    )?.[0] ?? "sent → delivered → read";

  const inject = cfg.inject_error ?? { on: "messages", code: 131047, times: 1 };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <Settings2 className="h-5 w-5" /> Behavior config
        </CardTitle>
        <CardDescription>
          The single switchboard (<code>PUT /sandbox/config</code>). &quot;auto&quot; = real
          production rules.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <Row label="GA mode" hint="post-GA: phones can be withheld">
          <Switch checked={cfg.ga_mode} onCheckedChange={(v) => patch((c) => (c.ga_mode = v))} />
        </Row>

        <Separator />
        <SectionTitle>Simulated end-user</SectionTitle>

        <Row label="Has username">
          <Switch
            checked={cfg.user.has_username}
            onCheckedChange={(v) => patch((c) => (c.user.has_username = v))}
          />
        </Row>
        {cfg.user.has_username && (
          <Row label="Username" hint='"auto" derives one'>
            <Input
              className="h-8 w-40"
              value={cfg.user.username}
              onChange={(e) => patch((c) => (c.user.username = e.target.value))}
            />
          </Row>
        )}
        <Row label="Phone visibility">
          <Select
            value={cfg.user.phone_visibility}
            onValueChange={(v) => patch((c) => (c.user.phone_visibility = v as never))}
          >
            <SelectTrigger className="h-8 w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">auto</SelectItem>
              <SelectItem value="always">always</SelectItem>
              <SelectItem value="never">never</SelectItem>
            </SelectContent>
          </Select>
        </Row>
        <Row label="In contact book">
          <Select
            value={String(cfg.user.in_contact_book)}
            onValueChange={(v) =>
              patch((c) => (c.user.in_contact_book = v === "auto" ? "auto" : v === "true"))
            }
          >
            <SelectTrigger className="h-8 w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">auto</SelectItem>
              <SelectItem value="true">yes</SelectItem>
              <SelectItem value="false">no</SelectItem>
            </SelectContent>
          </Select>
        </Row>
        <Row label="Service window" hint="24h customer-service window">
          <Select
            value={cfg.user.service_window}
            onValueChange={(v) => patch((c) => (c.user.service_window = v as never))}
          >
            <SelectTrigger className="h-8 w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">auto</SelectItem>
              <SelectItem value="open">open</SelectItem>
              <SelectItem value="closed">closed</SelectItem>
            </SelectContent>
          </Select>
        </Row>
        <Row label="Parent BSUID" hint="include parent_user_id fields">
          <Switch
            checked={cfg.user.parent_bsuid}
            onCheckedChange={(v) => patch((c) => (c.user.parent_bsuid = v))}
          />
        </Row>
        <Row label="Country">
          <Input
            className="h-8 w-20 uppercase"
            maxLength={2}
            value={cfg.user.country}
            onChange={(e) => patch((c) => (c.user.country = e.target.value.toUpperCase()))}
          />
        </Row>

        <Separator />
        <SectionTitle>Consumer actions (after each delivered send)</SectionTitle>

        <Row label="Reply to messages">
          <Switch
            checked={cfg.consumer_actions.reply_to_messages}
            onCheckedChange={(v) => patch((c) => (c.consumer_actions.reply_to_messages = v))}
          />
        </Row>
        {cfg.consumer_actions.reply_to_messages && (
          <>
            <Row label="Reply text">
              <Input
                className="h-8 w-40"
                value={cfg.consumer_actions.reply_text}
                onChange={(e) => patch((c) => (c.consumer_actions.reply_text = e.target.value))}
              />
            </Row>
            <Row label="Reply delay (ms)">
              <Input
                className="h-8 w-24"
                type="number"
                value={cfg.consumer_actions.reply_delay_ms}
                onChange={(e) =>
                  patch((c) => (c.consumer_actions.reply_delay_ms = Number(e.target.value) || 0))
                }
              />
            </Row>
          </>
        )}
        <Row label="Tap “Share Contact Info”" hint="on REQUEST_CONTACT_INFO messages">
          <Switch
            checked={cfg.consumer_actions.tap_request_contact_info}
            onCheckedChange={(v) =>
              patch((c) => (c.consumer_actions.tap_request_contact_info = v))
            }
          />
        </Row>
        <Row label="Share contact manually" hint="contacts webhook with vCard">
          <Switch
            checked={cfg.consumer_actions.share_contact_manually}
            onCheckedChange={(v) =>
              patch((c) => (c.consumer_actions.share_contact_manually = v))
            }
          />
        </Row>
        <Row label="Tap/share delay (ms)">
          <Input
            className="h-8 w-24"
            type="number"
            value={cfg.consumer_actions.tap_delay_ms}
            onChange={(e) =>
              patch((c) => (c.consumer_actions.tap_delay_ms = Number(e.target.value) || 0))
            }
          />
        </Row>

        <Separator />
        <SectionTitle>Status webhooks</SectionTitle>

        <Row label="Sequence">
          <Select
            value={seqLabel}
            onValueChange={(v) => patch((c) => (c.statuses.sequence = SEQUENCE_PRESETS[v]))}
          >
            <SelectTrigger className="h-8 w-52">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.keys(SEQUENCE_PRESETS).map((k) => (
                <SelectItem key={k} value={k}>
                  {k}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Row>
        <Row label="Delays (ms, comma-sep)">
          <Input
            className="h-8 w-32"
            value={cfg.statuses.delays_ms.join(",")}
            onChange={(e) =>
              patch(
                (c) =>
                  (c.statuses.delays_ms = e.target.value
                    .split(",")
                    .map((x) => Number(x.trim()) || 0))
              )
            }
          />
        </Row>
        {cfg.statuses.sequence.includes("failed") && (
          <Row label="Failed error code">
            <Input
              className="h-8 w-28"
              type="number"
              value={cfg.statuses.failed_error_code}
              onChange={(e) =>
                patch((c) => (c.statuses.failed_error_code = Number(e.target.value) || 131049))
              }
            />
          </Row>
        )}

        <Separator />
        <SectionTitle>Error injection</SectionTitle>

        <Row label="Inject an error" hint="fail the next N matching calls">
          <Switch checked={injectOn} onCheckedChange={setInjectOn} />
        </Row>
        {injectOn && (
          <div className="flex items-center gap-2">
            <Select
              value={inject.on}
              onValueChange={(v) => patch((c) => (c.inject_error = { ...inject, on: v }))}
            >
              <SelectTrigger className="h-8 w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="messages">messages</SelectItem>
                <SelectItem value="marketing_messages">marketing_messages</SelectItem>
                <SelectItem value="username">username</SelectItem>
              </SelectContent>
            </Select>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">code</Label>
              <Input
                className="h-8 w-24"
                type="number"
                value={inject.code}
                onChange={(e) =>
                  patch((c) => (c.inject_error = { ...inject, code: Number(e.target.value) || 0 }))
                }
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">times</Label>
              <Input
                className="h-8 w-16"
                type="number"
                value={inject.times}
                onChange={(e) =>
                  patch((c) => (c.inject_error = { ...inject, times: Number(e.target.value) || 1 }))
                }
              />
            </div>
          </div>
        )}

        <Separator />
        <div className="flex items-center gap-2">
          <Button onClick={save} disabled={busy}>
            <Save /> Save config
          </Button>
          <Button variant="outline" onClick={load}>
            <RefreshCw /> Reload
          </Button>
        </div>
        {status && (
          <p className={`text-sm ${status.kind === "ok" ? "text-emerald-600" : "text-destructive"}`}>
            {status.text}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
