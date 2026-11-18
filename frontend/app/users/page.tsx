"use client";
import { useCallback, useEffect, useState } from "react";
import { api, ago, patch, post } from "@/lib/api";
import { can, useMe } from "@/components/auth";

type U = { id: number; username: string; role: string; is_active: boolean; created_by: string; created_at: string; last_login_at: string | null };
const ROLES = ["viewer", "approver", "admin"];
const input = "rounded border border-edge bg-ink px-2 py-1.5 text-sm";

export default function UsersPage() {
  const me = useMe();
  const [users, setUsers] = useState<U[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [form, setForm] = useState({ username: "", password: "", role: "approver" });
  const [resetId, setResetId] = useState<number | null>(null);
  const [resetPw, setResetPw] = useState("");

  const load = useCallback(async () => {
    try { setUsers(await api<U[]>("/api/users")); setError(null); } catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { if (can(me, "admin")) load(); }, [me, load]);

  async function run(fn: () => Promise<unknown>, ok: string) {
    setNotice(null); setError(null);
    try { await fn(); setNotice(ok); await load(); } catch (e) { setError((e as Error).message); }
  }

  if (!can(me, "admin")) return <div className="card text-muted">Only administrators can manage users.</div>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Users</h1>
        <p className="text-sm text-muted">
          Accounts sign in with a username and password. There is no public sign-up: only administrators create accounts.
          The reviewer recorded on an approval is the username.
        </p>
      </div>
      {error && <div className="card border-rose-500/50 text-rose-300">{error}</div>}
      {notice && <div className="card text-emerald-300">{notice}</div>}

      <form className="card flex flex-wrap items-end gap-3" onSubmit={(e) => {
        e.preventDefault();
        run(async () => { await post("/api/users", form); setForm({ username: "", password: "", role: form.role }); }, `Created ${form.username}`);
      }}>
        <div className="card-title w-full">Add a user</div>
        <label className="text-xs text-muted">Username
          <input className={`${input} mt-1 block`} value={form.username} autoComplete="off"
            onChange={(e) => setForm({ ...form, username: e.target.value })} />
        </label>
        <label className="text-xs text-muted">Initial password (min 10 characters)
          <input type="password" className={`${input} mt-1 block`} value={form.password} autoComplete="new-password"
            onChange={(e) => setForm({ ...form, password: e.target.value })} />
        </label>
        <label className="text-xs text-muted">Role
          <select className={`${input} mt-1 block`} value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
            {ROLES.map((r) => <option key={r}>{r}</option>)}
          </select>
        </label>
        <button className="btn-primary" disabled={!form.username.trim() || form.password.length < 10}>Create user</button>
      </form>

      <div className="card overflow-x-auto">
        <div className="card-title">Accounts</div>
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-muted">
            <tr><th className="py-2">Username</th><th>Role</th><th>Status</th><th>Last sign-in</th><th>Created by</th><th /></tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const self = me?.auth_method === "session" && me.name === u.username;
              return (
                <tr key={u.id} className="border-t border-edge align-top">
                  <td className="py-2 font-medium">{u.username}{self && <span className="ml-2 text-xs text-muted">(you)</span>}</td>
                  <td>
                    <select className={input} value={u.role} disabled={self} aria-label={`Role for ${u.username}`}
                      onChange={(e) => run(() => patch(`/api/users/${u.id}`, { role: e.target.value }), `Updated ${u.username}`)}>
                      {ROLES.map((r) => <option key={r}>{r}</option>)}
                    </select>
                  </td>
                  <td>{u.is_active ? <span className="text-emerald-300">active</span> : <span className="text-rose-300">disabled</span>}</td>
                  <td className="text-muted">{u.last_login_at ? ago(u.last_login_at) : "never"}</td>
                  <td className="text-muted">{u.created_by || "—"}</td>
                  <td className="space-x-3 whitespace-nowrap text-right">
                    {resetId === u.id ? (
                      <span className="inline-flex gap-2">
                        <input type="password" className={input} placeholder="New password" value={resetPw} autoComplete="new-password"
                          onChange={(e) => setResetPw(e.target.value)} />
                        <button className="btn-primary" disabled={resetPw.length < 10}
                          onClick={() => run(async () => { await patch(`/api/users/${u.id}`, { password: resetPw }); setResetId(null); setResetPw(""); }, `Password reset for ${u.username}`)}>Save</button>
                        <button className="btn-ghost" onClick={() => { setResetId(null); setResetPw(""); }}>Cancel</button>
                      </span>
                    ) : (
                      <>
                        <button className="text-xs underline hover:text-slate-100" onClick={() => { setResetId(u.id); setResetPw(""); }}>Reset password</button>
                        {!self && (
                          <button className="text-xs underline hover:text-slate-100"
                            onClick={() => run(() => patch(`/api/users/${u.id}`, { is_active: !u.is_active }), `${u.is_active ? "Disabled" : "Enabled"} ${u.username}`)}>
                            {u.is_active ? "Disable" : "Enable"}
                          </button>
                        )}
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
            {users.length === 0 && <tr><td colSpan={6} className="py-4 text-muted">No accounts yet. Create your own above, then sign out and sign in with it.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
