export type KeyInfo = {
  d360_api_key: string;
  display_phone_number: string;
  waba_id: string;
  phone_number_id: string;
  expires_at: string;
};

export type SandboxConfig = {
  ga_mode: boolean;
  user: {
    has_username: boolean;
    username: string;
    country: string;
    phone_visibility: "auto" | "always" | "never";
    in_contact_book: "auto" | boolean;
    service_window: "auto" | "open" | "closed";
    parent_bsuid: boolean;
  };
  consumer_actions: {
    reply_to_messages: boolean;
    reply_text: string;
    reply_delay_ms: number;
    tap_request_contact_info: boolean;
    tap_delay_ms: number;
    share_contact_manually: boolean;
  };
  statuses: {
    sequence: string[];
    delays_ms: number[];
    failed_error_code: number;
  };
  inject_error: { on: string; code: number; times: number } | null;
};

export type RequestRow = {
  id: string;
  method: string;
  path: string;
  status_code: number;
  error_code: number | null;
  request_body: string | null;
  response_body: string | null;
  duration_ms: number;
  created_at: string;
};

export type WebhookRow = {
  id: string;
  field: string;
  status: string;
  attempts: number;
  response_code: number | null;
  created_at: string;
  payload: unknown;
};

export const DEFAULT_BASE =
  process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, public body: unknown) {
    super(`HTTP ${status}`);
  }
}

export async function api<T = unknown>(
  base: string,
  path: string,
  opts: { method?: string; body?: unknown; key?: string } = {}
): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (opts.key) headers["D360-API-KEY"] = opts.key;
  const res = await fetch(`${base.replace(/\/$/, "")}${path}`, {
    method: opts.method ?? "GET",
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, json);
  return json as T;
}

export function errorDetails(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { error?: { error_data?: { details?: string }; message?: string } } | null;
    return body?.error?.error_data?.details || body?.error?.message || `HTTP ${e.status}`;
  }
  return e instanceof Error ? e.message : String(e);
}
