"use client";
import { useCallback, useEffect, useRef, useState } from "react";

declare global {
  interface Window {
    __APP_CONFIG__?: { apiUrl?: string };
  }
}

/**
 * API base URL, resolved at *runtime*: the server layout injects window.__APP_CONFIG__ from the container's
 * API_URL env var, so one built image works for any domain. NEXT_PUBLIC_API_URL remains a build-time fallback.
 */
export function apiUrl(): string {
  const runtime = typeof window !== "undefined" ? window.__APP_CONFIG__?.apiUrl : undefined;
  return (runtime || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/+$/, "");
}

const KEY_STORAGE = "sc_api_key";
export const AUTH_EVENT = "sc-auth-required";

export function getApiKey(): string {
  try { return localStorage.getItem(KEY_STORAGE) ?? ""; } catch { return ""; }
}
export function setApiKey(key: string) {
  try { key ? localStorage.setItem(KEY_STORAGE, key) : localStorage.removeItem(KEY_STORAGE); } catch {}
}

// A username/password login yields a session token (sent as a Bearer token). An API key is the alternative credential.
const SESSION_STORAGE = "sc_session";

export function getSession(): string {
  try { return localStorage.getItem(SESSION_STORAGE) ?? ""; } catch { return ""; }
}
export function setSession(token: string) {
  try { token ? localStorage.setItem(SESSION_STORAGE, token) : localStorage.removeItem(SESSION_STORAGE); } catch {}
}
export function clearCredentials() {
  setSession("");
  setApiKey("");
}

export function authHeaders(extra?: HeadersInit): Record<string, string> {
  const base = { ...(extra as Record<string, string>) };
  const session = getSession();
  if (session) return { ...base, Authorization: `Bearer ${session}` };
  const key = getApiKey();
  return { ...base, ...(key ? { "X-API-Key": key } : {}) };
}

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${apiUrl()}${path}`, {
    ...init,
    headers: authHeaders({ "Content-Type": "application/json", ...(init?.headers as any) }),
    cache: "no-store",
  });
  if (!res.ok) {
    if (res.status === 401 && typeof window !== "undefined") window.dispatchEvent(new Event(AUTH_EVENT));
    const body = await res.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : undefined;
    throw new ApiError(detail ?? `${res.status} ${res.statusText}`, res.status);
  }
  return res.json();
}

export const post = <T = any>(path: string, body: unknown = {}) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const patch = <T = any>(path: string, body: unknown = {}) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });

/** Username + password sign-in. On success the session token is stored and used for all later requests. */
export async function loginWithPassword(username: string, password: string) {
  const res = await post<{ token: string; user: { username: string; role: string } }>(
    "/api/auth/login", { username, password });
  setApiKey("");
  setSession(res.token);
  return res.user;
}

// ---------------------------------------------------------------------------------------------------------
// Live updates over Server-Sent Events (fetch-based so we can send the API key header).
// ---------------------------------------------------------------------------------------------------------

/** Incrementally parse an SSE byte stream; returns [events, remainingBuffer]. */
export function parseSse(buffer: string): [{ event: string; data: string }[], string] {
  const events: { event: string; data: string }[] = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":") || !line) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    if (data.length || event !== "message") events.push({ event, data: data.join("\n") });
  }
  return [events, rest];
}

type Listener = (live: boolean) => void;
const listeners = new Set<Listener>();
const changeListeners = new Set<() => void>();
let started = false;
let isLive = false;
let abort: AbortController | null = null;

async function streamLoop() {
  let delay = 1000;
  for (;;) {
    abort = new AbortController();
    try {
      const res = await fetch(`${apiUrl()}/api/stream`, { headers: authHeaders(), signal: abort.signal, cache: "no-store" });
      if (!res.ok || !res.body) throw new Error(String(res.status));
      isLive = true; listeners.forEach((l) => l(true)); delay = 1000;
      changeListeners.forEach((l) => l()); // catch up on anything missed while disconnected
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const [events, rest] = parseSse(buf);
        buf = rest;
        if (events.some((e) => e.event === "change")) changeListeners.forEach((l) => l());
      }
    } catch { /* fall through to reconnect */ }
    isLive = false; listeners.forEach((l) => l(false));
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(delay * 2, 15000);
  }
}

function ensureStream() {
  if (started || typeof window === "undefined") return;
  started = true;
  streamLoop();
}

/** Restart the stream, e.g. after the user signed in or out. */
export function restartStream() {
  abort?.abort();
}

/**
 * Fetches an endpoint and refreshes it whenever the backend signals a change (SSE). Falls back to polling
 * at `intervalMs` while the stream is down, and to a slow safety poll while it is up.
 */
export function usePoll<T = any>(path: string, intervalMs = 3000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const live = useRef(false);
  const refresh = useCallback(async () => {
    try {
      setData(await api<T>(path));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [path]);

  useEffect(() => {
    ensureStream();
    live.current = isLive;
    const onLive: Listener = (v) => { live.current = v; };
    listeners.add(onLive);
    const onChange = () => { refresh(); };
    changeListeners.add(onChange);
    refresh();
    const id = setInterval(() => { if (!live.current || Math.random() < 0.1) refresh(); }, intervalMs);
    return () => { clearInterval(id); listeners.delete(onLive); changeListeners.delete(onChange); };
  }, [refresh, intervalMs]);
  return { data, error, refresh };
}

export const usd = (n?: number | null) =>
  n == null ? "—" : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export const num = (n?: number | null, digits = 0) =>
  n == null ? "—" : n.toLocaleString("en-US", { maximumFractionDigits: digits });

export const ago = (iso?: string | null) => {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};
