"use client";
import Link from "next/link";
import { useState } from "react";
import { ago, num, post, usd, usePoll } from "@/lib/api";

export default function OperationsPage() {
  const { data: pos, refresh } = usePoll<any[]>("/api/purchase-orders");
  const { data: inventory } = usePoll<any[]>("/api/inventory", 5000);
  const { data: shipments } = usePoll<any[]>("/api/shipments", 5000);
  const { data: signals } = usePoll<any[]>("/api/signals", 5000);
  const { data: events } = usePoll<any[]>("/api/events");
  const [busy, setBusy] = useState<number | null>(null);

  async function receive(id: number) {
    setBusy(id);
    await post(`/api/purchase-orders/${id}/receive`, {}).catch((e) => alert(e.message));
    setBusy(null);
    refresh();
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Operations</h1>

      <div className="card">
        <div className="card-title">ERP write-backs (POs, expedites, transfers)</div>
        {!pos?.length ? <p className="text-sm text-muted">No orders yet.</p> : (
          <table className="data">
            <thead><tr><th>Order</th><th>Kind</th><th>Supplier / route</th><th className="text-right">Qty</th><th className="text-right">Total</th><th>Status</th><th /></tr></thead>
            <tbody>
              {pos.map((p) => (
                <tr key={p.id}>
                  <td>{p.po_number}<div className="text-xs text-muted"><Link className="hover:text-accent" href={`/disruptions/${p.disruption_id}`}>disruption #{p.disruption_id}</Link> · {ago(p.created_at)}</div></td>
                  <td>{p.kind}</td>
                  <td>{p.supplier ?? "inter-warehouse"} → {p.warehouse}</td>
                  <td className="text-right tabular-nums">{num(p.quantity)} {p.product}</td>
                  <td className="text-right tabular-nums">{usd(p.total_cost)}</td>
                  <td className={p.status === "received" ? "text-emerald-300" : "text-sky-300"}>{p.status}{p.actual_days != null && <div className="text-xs text-muted">{num(p.actual_days, 1)}d actual</div>}</td>
                  <td>{p.status === "submitted" && <button className="btn-ghost" disabled={busy === p.id} onClick={() => receive(p.id)}>Receive</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="card">
          <div className="card-title">Inventory cover</div>
          <table className="data">
            <thead><tr><th>SKU</th><th>DC</th><th className="text-right">On hand</th><th className="text-right">Reorder pt</th><th className="text-right">Cover</th></tr></thead>
            <tbody>
              {(inventory ?? []).map((r) => (
                <tr key={`${r.sku}-${r.warehouse}`}>
                  <td>{r.sku}</td><td>{r.warehouse}</td>
                  <td className="text-right tabular-nums">{num(r.on_hand)}</td>
                  <td className="text-right tabular-nums">{num(r.reorder_point)}</td>
                  <td className={`text-right tabular-nums ${r.on_hand < r.reorder_point ? "text-amber-200" : ""}`}>{r.days_of_cover}d</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="space-y-6">
          <div className="card">
            <div className="card-title">Inbound shipments</div>
            <table className="data">
              <thead><tr><th>Shipment</th><th>Lane</th><th className="text-right">Qty</th><th className="text-right">Delay</th></tr></thead>
              <tbody>
                {(shipments ?? []).map((s) => (
                  <tr key={s.code}>
                    <td>{s.code}<div className="text-xs text-muted">{s.carrier} {s.vessel} · {s.status}</div></td>
                    <td className="text-xs">{s.supplier} → {s.warehouse}</td>
                    <td className="text-right tabular-nums">{num(s.quantity)} {s.sku}</td>
                    <td className={`text-right tabular-nums ${s.delay_days > 0 ? "text-rose-300" : ""}`}>{s.delay_days > 0 ? `+${s.delay_days}d` : "on time"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card">
            <div className="card-title">External risk signals</div>
            {!signals?.length ? <p className="text-sm text-muted">None.</p> : signals.map((s) => (
              <div key={s.id} className={`mb-2 text-sm ${s.active ? "" : "opacity-50"}`}>
                <span className="font-medium">{s.kind.replace(/_/g, " ")}</span> · {s.region} · severity {s.severity}
                <div className="text-xs text-muted">{s.description} ({s.source}{s.active ? "" : ", expired"})</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-title">Event stream</div>
        <table className="data">
          <thead><tr><th>Received</th><th>Type</th><th>Source</th><th>Status</th><th>Result</th></tr></thead>
          <tbody>
            {(events ?? []).map((e) => (
              <tr key={e.event_id}>
                <td className="text-xs">{ago(e.received_at)}</td>
                <td className="font-mono text-xs">{e.type}</td>
                <td className="text-xs">{e.source}</td>
                <td className="text-xs">{e.status}</td>
                <td className="text-xs text-muted">
                  {e.result?.error ?? (e.result?.anomalies != null ? `${e.result.anomalies} anomal${e.result.anomalies === 1 ? "y" : "ies"}` : "")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
