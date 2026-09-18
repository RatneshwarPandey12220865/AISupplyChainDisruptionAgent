"use client";
import { ago, num, usePoll } from "@/lib/api";

export default function SuppliersPage() {
  const { data } = usePoll<any[]>("/api/suppliers", 5000);
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Supplier reliability</h1>
        <p className="text-sm text-muted">Scores are updated automatically by post-mortems (EWMA of on-time performance) and feed future sourcing decisions and guardrails.</p>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        {(data ?? []).map((s) => (
          <div key={s.id} className="card">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-semibold">{s.name}</div>
                <div className="text-xs text-muted">{s.country} · {s.region} · {s.port}</div>
              </div>
              <div className="text-right">
                <div className={`text-2xl font-semibold tabular-nums ${s.reliability_score >= 0.85 ? "text-emerald-300" : s.reliability_score >= 0.7 ? "text-amber-200" : "text-rose-300"}`}>
                  {(s.reliability_score * 100).toFixed(1)}
                </div>
                <div className="text-xs text-muted">{s.status.replace(/_/g, " ")}</div>
              </div>
            </div>
            <div className="mt-3 h-1.5 rounded bg-edge">
              <div className="h-1.5 rounded bg-accent" style={{ width: `${s.reliability_score * 100}%` }} />
            </div>
            <table className="data mt-3">
              <thead><tr><th>SKU</th><th className="text-right">Price</th><th className="text-right">Lead time</th><th className="text-right">Capacity/wk</th></tr></thead>
              <tbody>
                {s.catalog.map((c: any) => (
                  <tr key={c.sku}>
                    <td>{c.sku}</td>
                    <td className="text-right tabular-nums">${c.unit_price.toFixed(2)}</td>
                    <td className="text-right tabular-nums">{num(c.lead_time_days)}d</td>
                    <td className="text-right tabular-nums">{num(c.capacity_per_week)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!!s.score_history.length && (
              <div className="mt-3 space-y-1 text-xs text-muted">
                {s.score_history.map((h: any, i: number) => (
                  <div key={i}>
                    {ago(h.at)}: {h.old.toFixed(3)} → <span className={h.new < h.old ? "text-rose-300" : "text-emerald-300"}>{h.new.toFixed(3)}</span> — {h.reason}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      {!data && <p className="text-muted">Loading…</p>}
    </div>
  );
}
