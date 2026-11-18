"use client";
import Link from "next/link";
import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import {
  api, ApiError, AUTH_EVENT, clearCredentials, getApiKey, getSession, loginWithPassword, post, restartStream, setApiKey,
  setSession,
} from "@/lib/api";

export type Me = {
  name: string; role: "viewer" | "approver" | "admin"; auth_enabled: boolean; auth_method?: "api_key" | "session" | "dev";
  demo_mode: boolean; admin_reset: boolean;
};

const RANK = { viewer: 1, approver: 2, admin: 3 } as const;
const MeContext = createContext<Me | null>(null);

export const useMe = () => useContext(MeContext);
export const can = (me: Me | null, role: keyof typeof RANK) => !!me && RANK[me.role] >= RANK[role];

const input = "w-full rounded border border-edge bg-ink px-2 py-1.5 text-sm";

/** Validates the login against /api/me; shows a username/password form (or an API-key form) when required. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [unreachable, setUnreachable] = useState<string | null>(null);
  const [mode, setMode] = useState<"password" | "key">("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [keyDraft, setKeyDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const check = useCallback(async () => {
    try {
      setMe(await api<Me>("/api/me"));
      setNeedsLogin(false);
      setUnreachable(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setNeedsLogin(true);
      else setUnreachable((e as Error).message);
    }
  }, []);

  useEffect(() => {
    check();
    const onAuth = () => { setMe(null); setNeedsLogin(true); };
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, [check]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      if (mode === "password") await loginWithPassword(username.trim(), password);
      else { setSession(""); setApiKey(keyDraft.trim()); }
      setMe(await api<Me>("/api/me"));
      setNeedsLogin(false);
      setPassword("");
      setKeyDraft("");
      restartStream();
    } catch (e2) {
      if (mode === "key") setApiKey("");
      setErr((e2 as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    if (getSession()) { try { await post("/api/auth/logout"); } catch { /* already invalid */ } }
    clearCredentials();
    setMe(null);
    setMode("password"); // always come back to the normal sign-in form
    setErr(null);
    setNeedsLogin(true);
    restartStream();
  }

  if (needsLogin) {
    const keyMode = mode === "key";
    return (
      <form onSubmit={submit} className="card mx-auto mt-16 max-w-md space-y-3">
        <div className="card-title">Sign in</div>
        {keyMode ? (
          <>
            <p className="text-sm text-muted">
              Enter an API key. Administrators can use their key to create user accounts, then sign in with a username.
            </p>
            <input type="password" autoFocus autoComplete="off" value={keyDraft} onChange={(e) => setKeyDraft(e.target.value)}
              placeholder="API key" aria-label="API key" className={input} />
          </>
        ) : (
          <>
            <p className="text-sm text-muted">Sign in with your username and password. Ask an administrator for an account.</p>
            <input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)}
              placeholder="Username" aria-label="Username" className={input} />
            <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)}
              placeholder="Password" aria-label="Password" className={input} />
          </>
        )}
        {err && <div className="text-sm text-rose-300">{err}</div>}
        <div className="flex items-center justify-between">
          <button className="btn-primary" disabled={busy || (keyMode ? !keyDraft.trim() : !username.trim() || !password)}>
            {keyMode ? "Continue" : "Sign in"}
          </button>
          <button type="button" className="text-xs text-muted underline hover:text-slate-100"
            onClick={() => { setErr(null); setMode(keyMode ? "password" : "key"); }}>
            {keyMode ? "Use username and password" : "Sign in with an API key instead"}
          </button>
        </div>
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
          {me.role === "admin" && <Link href="/users" className="underline hover:text-slate-100">Users</Link>}
          {me.auth_method === "session" && <Link href="/account" className="underline hover:text-slate-100">Account</Link>}
          <button className="underline hover:text-slate-100" onClick={signOut}>Sign out</button>
        </div>
      )}
      {children}
    </MeContext.Provider>
  );
}

export { getApiKey };
