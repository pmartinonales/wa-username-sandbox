"use client";

import { useState } from "react";
import { Check, Copy, KeyRound, Plus } from "lucide-react";
import { api, errorDetails, type KeyInfo } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function KeyPanel({
  base,
  keyInfo,
  onKey,
}: {
  base: string;
  keyInfo: KeyInfo | null;
  onKey: (k: KeyInfo | null) => void;
}) {
  const [name, setName] = useState("my-test");
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function createKey() {
    setBusy(true);
    setError(null);
    try {
      const info = await api<KeyInfo>(base, "/sandbox/keys", {
        method: "POST",
        body: { name },
      });
      onKey(info);
    } catch (e) {
      setError(errorDetails(e));
    } finally {
      setBusy(false);
    }
  }

  function usePasted() {
    if (!pasted.trim()) return;
    onKey({
      d360_api_key: pasted.trim(),
      display_phone_number: "(unknown)",
      waba_id: "(unknown)",
      phone_number_id: "(unknown)",
      expires_at: "",
    });
    setPasted("");
  }

  async function copy() {
    if (!keyInfo) return;
    await navigator.clipboard.writeText(keyInfo.d360_api_key);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <KeyRound className="h-5 w-5" /> API key
        </CardTitle>
        <CardDescription>
          One key = one virtual business number + one simulated consumer.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {keyInfo ? (
          <div className="space-y-2 text-sm">
            <div className="flex items-center gap-2">
              <code className="flex-1 truncate rounded bg-muted px-2 py-1 font-mono text-xs">
                {keyInfo.d360_api_key}
              </code>
              <Button variant="outline" size="icon" className="h-8 w-8 shrink-0" onClick={copy}>
                {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
              </Button>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>Business number</span>
              <span className="font-mono text-foreground">{keyInfo.display_phone_number}</span>
              <span>WABA ID</span>
              <span className="font-mono text-foreground">{keyInfo.waba_id}</span>
              <span>Phone number ID</span>
              <span className="font-mono text-foreground">{keyInfo.phone_number_id}</span>
              {keyInfo.expires_at && (
                <>
                  <span>Expires</span>
                  <span className="font-mono text-foreground">
                    {keyInfo.expires_at.slice(0, 10)}
                  </span>
                </>
              )}
            </div>
            <Button variant="ghost" size="sm" onClick={() => onKey(null)}>
              Switch key
            </Button>
          </div>
        ) : (
          <>
            <div className="flex items-end gap-2">
              <div className="flex-1 space-y-1.5">
                <Label htmlFor="key-name">Name</Label>
                <Input id="key-name" value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <Button onClick={createKey} disabled={busy}>
                <Plus /> Create key
              </Button>
            </div>
            <div className="flex items-end gap-2">
              <div className="flex-1 space-y-1.5">
                <Label htmlFor="key-paste">…or paste an existing key</Label>
                <Input
                  id="key-paste"
                  placeholder="sk_sandbox_…"
                  value={pasted}
                  onChange={(e) => setPasted(e.target.value)}
                />
              </div>
              <Button variant="outline" onClick={usePasted} disabled={!pasted.trim()}>
                Use
              </Button>
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
          </>
        )}
      </CardContent>
    </Card>
  );
}
