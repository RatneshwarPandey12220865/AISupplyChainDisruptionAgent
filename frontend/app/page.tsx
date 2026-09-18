"use client";
import Link from "next/link";
import { useState } from "react";
import { SupplyMap } from "@/components/charts";
import { Kpi, SeverityBadge, StatusBadge } from "@/components/ui";
import { ago, post, usd, usePoll } from "@/lib/api";

const SCENARIO_LABELS: Record<string, string> = {
  port_congestion: "East Asia port congestion",
  shipment_delay: "Vessel rollover (+10d)",
  supplier_lead_time: "Supplier lead-time spike",
  low_stock: "Low stock at Dallas",
  cargo_temperature: "Cargo temperature excursion",
  supplier_outage: "Supplier force majeure",
};

export default function ControlTower() {
  const { data: dash, error, refresh } = usePoll("/api/dashboard");
  const { data: health } = usePoll("/api/health", 15000);
  const { data: disruptions, refresh: refreshList } = usePoll<any[]>("/api/disruptions");
  const { data: notes } = usePoll<any[]>("/api/notifications");
  const { data: suppliers } = usePoll<any[]>("/api/suppliers", 10000);
  const { data: shipments } = usePoll<any[]>("/api/shipments", 5000);
  const { data: signals } = usePoll<any[]>("/api/signals", 5000);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  async function run(scenario: string) {
    setBusy(scenario);
    try {
      await post(`/api/simulate/${scenario}`);
      setToast(`Injected "${SCENARIO_LABELS[scenario] ?? scenario}" — agents are working…`);
      setTimeout(() => { refresh(); refreshList(); }, 1500);
    } catch (e) {
      setToast((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function reset() {
    if (!confirm("Reset all demo data?")) return;
    setBusy("reset");
    await post("/api/admin/reset").catch((e) => setToast(e.message));
    setBusy(null);
    refresh(); refreshList();
  }

  const k = dash?.kpis ?? {};
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Supply Chain Control Tower</h1>
          <p className="text-sm text-muted">Detection → Impact → Monte Carlo → Sourcing → Guardrails → Execution</p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="rounded-full border border-edge px-2 py-1">LLM: {health?.llm ?? "…"}</span>
          <span className="rounded-full border border-edge px-2 py-1">DB: {health?.database ?? "…"}</span>
          <span className="rounded-full border border-edge px-2 py-1">Bus: {health?.event_bus ?? "…"}</span>
          <span className="rounded-full border border-edge px-2 py-1">Auto-approve ≤ {usd(health?.auto_approve_limit)}</span>
        </div>
      </div>

      {error && <div className="card border-rose-500/50 text-rose-300">Backend unreachable: {error}</div>}
      {toast && (
        <div className="card flex items-center justify-between border-sky-500/40 text-sky-200">
          {toast}
          <button className="text-xs text-muted" onClick={() => setToast(null)}>dismiss</button>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Kpi label="Open disruptions" value={k.open_disruptions ?? "—"} />
        <Kpi label="Awaiting approval" value={k.awaiting_approval ?? "—"} tone={k.awaiting_approval ? "text-fuchsia-300" : ""} />
        <Kpi label="Value at risk" value={usd(k.value_at_risk)} tone="text-rose-300" hint="Expected loss, unmitigated" />
        <Kpi label="Auto-executed" value={k.auto_executed ?? "—"} tone="text-emerald-300" />
        <Kpi label="Mitigation spend" value={usd(k.mitigation_spend)} />
        <Kpi label="Active risk signals" value={k.active_signals ?? "—"} />
      </div>

      <div className="card">
        <div className="card-title">Inject a disruption (demo)</div>
        <div className="flex flex-wrap gap-2">
          {(dash?.scenarios ?? []).map((s: string) => (
            <button key={s} className="btn-ghost" disabled={!!busy} onClick={() => run(s)}>
              {busy === s ? "Sending…" : SCENARIO_LABELS[s] ?? s}
            </button>
          ))}
          <button className="btn-danger ml-auto" disabled={!!busy} onClick={reset}>Reset demo data</button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="card lg:col-span-2">
          <div className="card-title">Disruptions</div>
          {!disruptions?.length ? (
            <p className="py-8 text-center text-sm text-muted">No disruptions detected. Inject a scenario above.</p>
          ) : (
            <table className="data">
              <thead>
                <tr><th>Severity</th><th>Disruption</th><th>Status</th><th className="text-right">Value at risk</th><th>Plan</th></tr>
              </thead>
              <tbody>
                {disruptions.map((d) => (
                  <tr key={d.id} className="hover:bg-edge/40">
                    <td><SeverityBadge value={d.severity} /></td>
                    <td>
                      <Link href={`/disruptions/${d.id}`} className="font-medium hover:text-accent">{d.title}</Link>
                      <div className="text-xs text-muted">{d.product} · {d.warehouse} · {ago(d.created_at)}</div>
                    </td>
                    <td><StatusBadge value={d.status} /></td>
                    <td className="text-right tabular-nums">{usd(d.value_at_risk)}</td>
                    <td className="text-xs">
                      {d.recommended ?? "—"}
                      {d.plan_cost != null && <div className="text-muted">{usd(d.plan_cost)}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="space-y-6">
          <div className="card">
            <div className="card-title">Supply network</div>
            <SupplyMap suppliers={suppliers ?? []} shipments={shipments ?? []} signals={signals ?? []} />
            <div className="mt-2 flex gap-4 text-xs text-muted">
              <span><span className="text-emerald-300">●</span> supplier</span>
              <span><span className="text-rose-300">●</span> at risk</span>
              <span><span className="text-rose-300">- -</span> delayed lane</span>
            </div>
          </div>
          <div className="card">
            <div className="card-title">Alerts</div>
            <ul className="max-h-96 space-y-3 overflow-auto text-sm">
              {(notes ?? []).map((n) => (
                <li key={n.id} className="border-l-2 pl-3" style={{ borderColor: n.level === "critical" ? "#fb7185" : n.level === "warning" ? "#fbbf24" : "#38bdf8" }}>
                  <Link href={n.disruption_id ? `/disruptions/${n.disruption_id}` : "#"} className="font-medium hover:text-accent">{n.title}</Link>
                  <div className="text-xs text-muted">{ago(n.created_at)} · {n.channels.join(", ")}</div>
                </li>
              ))}
              {!notes?.length && <li className="text-muted">No alerts yet.</li>}
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
