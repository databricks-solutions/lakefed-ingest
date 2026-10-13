// Thin client for the FastAPI backend (main.py). Errors carry the server's detail and hint.

export type Row = Record<string, any>;

export class ApiError extends Error {
  hint?: string;
  constructor(message: string, hint?: string) {
    super(message);
    this.hint = hint;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail;
    const message =
      typeof detail === "string" ? detail
      : detail?.errors ? detail.errors.join("; ")
      : Array.isArray(detail) ? detail.map((d: any) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : `${res.status} ${res.statusText}`;
    throw new ApiError(message, data?.hint ?? undefined);
  }
  return data as T;
}

export const api = {
  get: <T = Row[]>(path: string) => request<T>("GET", path),
  post: <T = Row>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
};

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export interface Config {
  lakebase_instance: string;
  lakebase_database: string;
  user: string;
  jobs: { ingest?: string; setup?: string; discover?: string };
  defaults: { partition_threshold_mb: number; partition_size_mb: number };
}
