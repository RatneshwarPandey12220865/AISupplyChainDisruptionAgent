"use client";
import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { api, ApiError, AUTH_EVENT, getApiKey, restartStream, setApiKey } from "@/lib/api";

export type Me = { name: string; role: "viewer" | "approver" | "admin"; auth_enabled: boolean; demo_mode: boolean; admin_reset: boolean };

const RANK = { viewer: 1, approver: 2, admin: 3 } as const;
const MeContext = createContext<Me | null>(null);

export const useMe = () => useContext(MeContext);
export const can = (me: Me | null, role: keyof typeof RANK) => !!me && RANK[me.role] >= RANK[role];

/** Validates the session against /api/me and shows a key prompt when the backend requires authentication. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [needsKey, setNeedsKey] = useState(false);
  const [unreachable, setUnreachable] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const check = useCallback(async () => {
    try {
      setMe(await api<Me>("/api/me"));
      setNeedsKey(false);
      setUnreachable(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setNeedsKey(true);
      else setUnreachable((e as Error).message);
    }
  }, []);

  useEffect(() => {
    check();
    const onAuth = () => { setMe(null); setNeedsKey(true); };
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, [check]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setApiKey(draft.trim());
    try {
      setMe(await api<Me>("/api/me"));
      setNeedsKey(false);
      setDraft("");
      restartStream();
    } catch (e2) {
      setApiKey("");
      setErr((e2 as Error).message);
    }
  }

  function signOut() {
    setApiKey("");
    setMe(null);
    setNeedsKey(true);
    restartStream();
  }

  if (needsKey) {
    return (
      <form onSubmit={submit} className="card mx-auto mt-16 max-w-md space-y-3">
        <div className="card-title">Sign in</div>
        <p className="text-sm text-muted">Enter your API key. Ask an administrator for one if you don't have it.</p>
        <input
          type="password" autoFocus autoComplete="off" value={draft} onChange={(e) => setDraft(e.target.value)}
          placeholder="API key" aria-label="API key"
          className="w-full rounded border border-edge bg-ink px-2 py-1.5 text-sm"
        />
        {err && <div className="text-sm text-rose-300">{err}</div>}
        <button className="btn-primary" disabled={!draft.trim()}>Continue</button>
      </form>
    );
  }
  if (unreachable && !me) {
    return <div className="card border-rose-500/50 text-rose-300">Backend unreachable: {unreachable}</div>;
  }
  return (
    <MeContext.Provider value={me}>
      {me?.auth_enabled && (
        <div className="mb-4 flex items-center justify-end gap-3 text-xs text-muted">
          <span>{me.name} · {me.role}</span>
          <button className="underline hover:text-slate-100" onClick={signOut}>Sign out</button>
        </div>
      )}
      {children}
    </MeContext.Provider>
  );
}

export { getApiKey };
