// Fetch wrapper for featxd. Every API call carries the token kept in localStorage; a 401
// drops the token so the app falls back to the login page.

const TOKEN_KEY = "featx-token";
const listeners = new Set<() => void>();

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token.trim());
  else localStorage.removeItem(TOKEN_KEY);
  listeners.forEach((fn) => fn());
}

export function onTokenChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

export function query(params?: Params): string {
  if (!params) return "";
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method,
    signal,
    headers: { "X-Featx-Token": getToken(), ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const type = res.headers.get("content-type") ?? "";
  const payload = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    if (res.status === 401) setToken("");
    const msg = typeof payload === "string" ? payload : payload?.error ?? JSON.stringify(payload);
    throw new ApiError(msg || `${res.status} ${res.statusText}`, res.status);
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, params?: Params, signal?: AbortSignal) => request<T>("GET", path + query(params), undefined, signal),
  post: <T>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  put: <T>(path: string, body: unknown = {}) => request<T>("PUT", path, body),
  del: <T>(path: string, params?: Params) => request<T>("DELETE", path + query(params)),
};

/** Check a token without storing it (login form). */
export async function probeToken(token: string): Promise<boolean> {
  const res = await fetch("/status", { headers: { "X-Featx-Token": token.trim() } });
  return res.ok;
}
