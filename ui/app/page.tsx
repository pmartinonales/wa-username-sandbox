"use client";

import { useEffect, useState } from "react";
import { Boxes } from "lucide-react";
import { DEFAULT_BASE, type KeyInfo } from "@/lib/api";
import { ConfigPanel } from "@/components/config-panel";
import { InboundPanel } from "@/components/inbound-panel";
import { KeyPanel } from "@/components/key-panel";
import { Monitor } from "@/components/monitor";
import { SendPanel } from "@/components/send-panel";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const KEY_STORAGE = "username-sandbox.key";
const BASE_STORAGE = "username-sandbox.base";

export default function Page() {
  const [base, setBase] = useState(DEFAULT_BASE);
  const [keyInfo, setKeyInfo] = useState<KeyInfo | null>(null);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    const storedBase = localStorage.getItem(BASE_STORAGE);
    if (storedBase) setBase(storedBase);
    const storedKey = localStorage.getItem(KEY_STORAGE);
    if (storedKey) {
      try {
        setKeyInfo(JSON.parse(storedKey));
      } catch {
        localStorage.removeItem(KEY_STORAGE);
      }
    }
    setHydrated(true);
  }, []);

  function onKey(k: KeyInfo | null) {
    setKeyInfo(k);
    if (k) localStorage.setItem(KEY_STORAGE, JSON.stringify(k));
    else localStorage.removeItem(KEY_STORAGE);
  }

  function onBase(value: string) {
    setBase(value);
    localStorage.setItem(BASE_STORAGE, value);
  }

  if (!hydrated) return null;

  return (
    <div className="mx-auto max-w-7xl space-y-4 p-4 md:p-6">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Boxes className="h-7 w-7" />
          <div>
            <h1 className="text-xl font-semibold leading-tight">username-sandbox</h1>
            <p className="text-sm text-muted-foreground">
              WhatsApp usernames/BSUID mock API — dashboard
            </p>
          </div>
        </div>
        <div className="flex items-end gap-2">
          <div className="space-y-1">
            <Label htmlFor="base" className="text-xs text-muted-foreground">
              API base URL
            </Label>
            <Input
              id="base"
              className="h-8 w-72 font-mono text-xs"
              value={base}
              onChange={(e) => onBase(e.target.value)}
            />
          </div>
          <a
            className="pb-1.5 text-sm text-muted-foreground underline-offset-4 hover:underline"
            href={`${base.replace(/\/$/, "")}/docs`}
            target="_blank"
            rel="noreferrer"
          >
            /docs
          </a>
        </div>
      </header>

      <div className="grid gap-4 lg:grid-cols-[24rem_1fr]">
        <div className="space-y-4">
          <KeyPanel base={base} keyInfo={keyInfo} onKey={onKey} />
          {keyInfo && <InboundPanel base={base} keyInfo={keyInfo} />}
          {keyInfo && <SendPanel base={base} keyInfo={keyInfo} />}
          {keyInfo && <ConfigPanel base={base} apiKey={keyInfo.d360_api_key} />}
        </div>
        <div className="flex min-h-[36rem] flex-col">
          {keyInfo ? (
            <Monitor base={base} keyInfo={keyInfo} />
          ) : (
            <div className="flex flex-1 items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
              Create or paste an API key to start monitoring.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
