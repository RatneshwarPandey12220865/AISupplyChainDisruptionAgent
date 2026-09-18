"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { LossHistogram, UrgencyCurve } from "@/components/charts";
import { SeverityBadge, Stat, StatusBadge } from "@/components/ui";
import { ago, num, post, usd, usePoll } from "@/lib/api";

const NODE_LABELS: Record<string, string> = {
  detection: "1 · Detection", impact: "2 · Impact", simulation: "Monte Carlo", sourcing: "3 · Sourcing",
  guardrail: "Guardrail", escalate: "Escalation", human_review: "Human review", execution: "4 · Execution",
  reject: "Rejected", monitor: "Monitor", postmortem: "Post-mortem",
};

export default function DisruptionPage() {
  const { id } = useParams<{ id: string }>();
  const { data: d, error, refresh } = usePoll(`/api/disruptions/${id}`, 2500);

  if (error) return <div className="card text-rose-300">{error}</div>;
  if (!d) return <div className="text-muted">Loading…</div>;

  const rec = d.recommendation ?? {};
  const imp = d.impact ?? {};
  const sim = d.simulation ?? {};
  const urgency = rec.urgency;

  return (
    <div className="space-y-6">
      <Link href="/" className="text-sm text-muted hover:text-slate-200">← Control tower</Link>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-3xl">
          <div className="mb-2 flex items-center gap-2">
            <SeverityBadge value={d.severity} />
            <StatusBadge value={d.status} />
            <span className="text-xs text-muted">#{d.id} · {d.type.replace(/_/g, " ")} · {ago(d.created_at)}</span>
          </div>
          <h1 className="text-2xl font-semibold">{d.title}</h1>
          <p className="mt-2 text-slate-300">{d.summary}</p>
          {d.triage?.root_cause && <p className="mt-1 text-sm text-muted">Root cause: {d.triage.root_cause} · triage by {d.triage.source}</p>}
        </div>
        <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm">
          <Stat label="Product" value={d.product} />
          <Stat label="Warehouse" value={d.warehouse} />
          <Stat label="Supplier" value={d.supplier ?? "—"} />
          <Stat label="Expected loss (no action)" value={<span className="text-rose-300">{usd(sim.expected_loss)}</span>} />
        </div>
      </div>

      {urgency?.headline && (
        <div className="card border-rose-500/40 bg-rose-500/5 text-rose-200">⚠ {urgency.headline}</div>
      )}

      {d.status === "awaiting_approval" && <ApprovalPanel d={d} onDone={refresh} />}

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="card">
          <div className="card-title">Impact analysis</div>
          <div className="grid grid-cols-3 gap-4 text-sm">
            <Stat label="On hand" value={num(imp.on_hand)} />
            <Stat label="Days of cover" value={num(imp.days_of_cover, 1)} />
            <Stat label="Replenishment ETA" value={imp.expected_arrival_day != null ? `day ${num(imp.expected_arrival_day, 1)}` : "none on order"} />
            <Stat label="Stockout window" value={imp.projection?.stockout_start_day != null ? `day ${imp.projection.stockout_start_day}–${imp.projection.stockout_end_day}` : "none"} />
            <Stat label="Shortfall (units)" value={num(imp.shortfall_units)} />
            <Stat label="Revenue at risk" value={usd(imp.revenue_at_risk)} />
          </div>
          {imp.narrative && <p className="mt-4 text-sm text-slate-300">{imp.narrative}</p>}
          {!!imp.orders_at_risk?.length && (
            <table className="data mt-4">
              <thead><tr><th>Order</th><th>Customer</th><th className="text-right">Qty</th><th className="text-right">Late</th></tr></thead>
              <tbody>
                {imp.orders_at_risk.map((o: any) => (
                  <tr key={o.order_number}>
                    <td>{o.order_number}</td>
                    <td>{o.customer} {o.is_vip && <span className="ml-1 rounded bg-amber-500/20 px-1 text-xs text-amber-200">VIP</span>}</td>
                    <td className="text-right tabular-nums">{num(o.quantity)}</td>
                    <td className="text-right tabular-nums">{num(o.days_late, 1)}d</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <div className="card-title">Monte Carlo risk ({num(sim.runs)} runs, {sim.horizon_days}-day window)</div>
          <LossHistogram sim={sim} />
          <div className="mt-3 grid grid-cols-4 gap-4 text-sm">
            <Stat label="Expected" value={usd(sim.expected_loss)} />
            <Stat label="P85" value={usd(sim.p85)} />
            <Stat label="P95" value={usd(sim.p95)} />
            <Stat label="P(any loss)" value={sim.prob_any_loss != null ? `${Math.round(sim.prob_any_loss * 100)}%` : "—"} />
          </div>
          {urgency?.curve && (
            <>
              <div className="card-title mt-5">Cost of waiting: {urgency.option_id}</div>
              <UrgencyCurve curve={urgency.curve} windowHours={urgency.act_within_hours} />
            </>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-title">Mitigation options (ranked by expected total cost)</div>
        <div className="overflow-x-auto">
          <table className="data">
            <thead>
              <tr><th>Option</th><th className="text-right">Spend</th><th className="text-right">Premium</th><th className="text-right">Residual E[loss]</th><th className="text-right">P85</th><th className="text-right">Total</th><th className="text-right">Reliability</th></tr>
            </thead>
            <tbody>
              {(d.options ?? []).map((o: any) => {
                const chosen = o.id === (d.execution?.option_id ?? rec.recommended_option_id);
                return (
                  <tr key={o.id} className={chosen ? "bg-sky-500/10" : ""}>
                    <td>
                      {chosen && <span className="mr-2 text-accent">★</span>}{o.label}
                      <div className="text-xs text-muted">{o.id}{o.lead_time_days ? ` · ${o.lead_time_days}d lead` : ""}</div>
                    </td>
                    <td className="text-right tabular-nums">{usd(o.total_cost)}</td>
                    <td className="text-right tabular-nums">{usd(o.premium_cost)}</td>
                    <td className="text-right tabular-nums">{usd(o.expected_loss)}</td>
                    <td className="text-right tabular-nums">{usd(o.p85_loss)}</td>
                    <td className="text-right font-medium tabular-nums">{usd(o.expected_total_cost)}</td>
                    <td className="text-right tabular-nums">{o.reliability != null ? o.reliability.toFixed(2) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="card">
          <div className="card-title">Agent recommendation · {rec.source}{rec.confidence != null && ` · confidence ${Math.round(rec.confidence * 100)}%`}</div>
          <p className="text-sm text-slate-200">{rec.rationale}</p>
          <List title="Risks" items={rec.risks} />
          <List title="Next steps" items={rec.next_steps} />
          <List title="Contract considerations" items={rec.contract_considerations} />
          {!!rec.clauses?.length && (
            <details className="mt-3 text-sm">
              <summary className="cursor-pointer text-muted">Retrieved contract clauses ({rec.clauses.length})</summary>
              {rec.clauses.map((c: any, i: number) => (
                <div key={i} className="mt-2 rounded border border-edge p-2">
                  <div className="text-xs text-muted">{c.supplier} · {c.title} · score {c.score}</div>
                  <div className="text-slate-300">{c.content}</div>
                </div>
              ))}
            </details>
          )}
          {d.guardrail?.reasons?.length > 0 && <List title="Escalation reasons" items={d.guardrail.reasons} />}
        </div>

        <div className="space-y-6">
          <ExecutionCard d={d} onChange={refresh} />
          <div className="card">
            <div className="card-title">Agent trace</div>
            <ol className="space-y-2 text-sm">
              {(d.trace ?? []).map((t: any, i: number) => (
                <li key={i} className="flex gap-3">
                  <span className="w-28 shrink-0 text-xs text-accent">{NODE_LABELS[t.node] ?? t.node}</span>
                  <span className="text-slate-300">{t.message}</span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>
    </div>
  );
}

function List({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div className="mt-3">
      <div className="text-xs text-muted">{title}</div>
      <ul className="ml-4 list-disc text-sm text-slate-300">{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  );
}

function ApprovalPanel({ d, onDone }: { d: any; onDone: () => void }) {
  const actionable = (d.options ?? []).filter((o: any) => o.kind !== "wait");
  const [optionId, setOptionId] = useState<string>(d.recommendation?.recommended_option_id);
  const [reviewer, setReviewer] = useState("");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    try { setReviewer(localStorage.getItem("reviewer") ?? ""); } catch {}
  }, []);

  async function decide(approved: boolean) {
    setBusy(true); setErr(null);
    try {
      try { localStorage.setItem("reviewer", reviewer); } catch {}
      await post(`/api/disruptions/${d.id}/decision`, { approved, option_id: optionId, reviewer: reviewer || "planner", comment });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card border-fuchsia-500/50">
      <div className="card-title text-fuchsia-300">Human approval required</div>
      <ul className="mb-3 ml-4 list-disc text-sm text-slate-300">
        {(d.guardrail?.reasons ?? []).map((r: string) => <li key={r}>{r}</li>)}
      </ul>
      <div className="space-y-2">
        {actionable.map((o: any) => (
          <label key={o.id} className="flex cursor-pointer items-center gap-3 rounded border border-edge p-2 text-sm hover:bg-edge/40">
            <input type="radio" name="opt" checked={optionId === o.id} onChange={() => setOptionId(o.id)} />
            <span className="flex-1">{o.label}{o.id === d.recommendation?.recommended_option_id && <span className="ml-2 text-xs text-accent">recommended</span>}</span>
            <span className="tabular-nums text-muted">{usd(o.total_cost)} spend · E[loss] {usd(o.expected_loss)}</span>
          </label>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <input className="rounded border border-edge bg-ink px-2 py-1.5 text-sm" placeholder="Your name" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
        <input className="min-w-64 flex-1 rounded border border-edge bg-ink px-2 py-1.5 text-sm" placeholder="Comment (optional)" value={comment} onChange={(e) => setComment(e.target.value)} />
        <button className="btn-primary" disabled={busy} onClick={() => decide(true)}>Approve & execute</button>
        <button className="btn-danger" disabled={busy} onClick={() => decide(false)}>Reject</button>
      </div>
      {err && <div className="mt-2 text-sm text-rose-300">{err}</div>}
    </div>
  );
}

function ExecutionCard({ d, onChange }: { d: any; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); } catch (e) { alert((e as Error).message); } finally { setBusy(false); onChange(); }
  };
  const pm = d.postmortem ?? {};
  const canResolve = ["auto_executed", "approved_executed", "monitoring"].includes(d.status);

  return (
    <div className="card">
      <div className="card-title">Execution & post-mortem</div>
      {!d.purchase_orders?.length && <p className="text-sm text-muted">No ERP orders written.</p>}
      {d.purchase_orders?.map((p: any) => (
        <div key={p.id} className="mb-2 flex items-center justify-between rounded border border-edge p-2 text-sm">
          <div>
            <div className="font-medium">{p.po_number} <span className="text-xs text-muted">({p.kind})</span></div>
            <div className="text-xs text-muted">{num(p.quantity)} × {d.product} · {usd(p.total_cost)} · promised {num(p.expected_days, 1)}d{p.actual_days != null && ` · actual ${num(p.actual_days, 1)}d`}</div>
          </div>
          {p.status === "submitted" ? (
            <button className="btn-ghost" disabled={busy} onClick={() => {
              const v = prompt("Actual delivery time in days (blank = elapsed time)", String(p.expected_days));
              if (v === null) return;
              act(() => post(`/api/purchase-orders/${p.id}/receive`, { actual_days: v === "" ? null : Number(v) }));
            }}>Mark received</button>
          ) : <StatusBadge value={p.status} />}
        </div>
      ))}
      {canResolve && (
        <button className="btn-ghost mt-2" disabled={busy} onClick={() => act(() => post(`/api/disruptions/${d.id}/resolve`, {}))}>
          Resolve & run post-mortem
        </button>
      )}
      {pm.summary && (
        <div className="mt-3 text-sm">
          <div className="text-slate-200">{pm.summary}</div>
          <List title="Lessons" items={pm.lessons} />
          {!!pm.score_changes?.length && (
            <div className="mt-2 text-xs text-muted">
              {pm.score_changes.map((c: any, i: number) => (
                <div key={i}>{c.supplier}: {c.old.toFixed(3)} → <span className={c.new < c.old ? "text-rose-300" : "text-emerald-300"}>{c.new.toFixed(3)}</span> — {c.reason}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
