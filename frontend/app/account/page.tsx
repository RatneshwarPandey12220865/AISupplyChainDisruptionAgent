"use client";
import { useState } from "react";
import { post } from "@/lib/api";
import { useMe } from "@/components/auth";

const input = "w-full rounded border border-edge bg-ink px-2 py-1.5 text-sm";

export default function AccountPage() {
  const me = useMe();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  if (me?.auth_method !== "session") {
    return <div className="card text-muted">Only accounts that signed in with a username can change a password here.</div>;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (next !== confirm) return setMsg({ ok: false, text: "The new passwords do not match." });
    try {
      await post("/api/auth/password", { current_password: current, new_password: next });
      setCurrent(""); setNext(""); setConfirm("");
      setMsg({ ok: true, text: "Password changed. Your other sessions were signed out." });
    } catch (e2) {
      setMsg({ ok: false, text: (e2 as Error).message });
    }
  }

  return (
    <form onSubmit={submit} className="card mx-auto mt-8 max-w-md space-y-3">
      <div className="card-title">Account: {me.name}</div>
      <input type="password" autoComplete="current-password" placeholder="Current password" aria-label="Current password"
        className={input} value={current} onChange={(e) => setCurrent(e.target.value)} />
      <input type="password" autoComplete="new-password" placeholder="New password (min 10 characters)" aria-label="New password"
        className={input} value={next} onChange={(e) => setNext(e.target.value)} />
      <input type="password" autoComplete="new-password" placeholder="Repeat new password" aria-label="Repeat new password"
        className={input} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      {msg && <div className={`text-sm ${msg.ok ? "text-emerald-300" : "text-rose-300"}`}>{msg.text}</div>}
      <button className="btn-primary" disabled={!current || next.length < 10}>Change password</button>
    </form>
  );
}
