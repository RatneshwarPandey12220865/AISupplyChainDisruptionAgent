import { ReactNode } from "react";

const SEVERITY: Record<string, string> = {
  critical: "bg-rose-500/15 text-rose-300 ring-rose-500/40",
  high: "bg-orange-500/15 text-orange-300 ring-orange-500/40",
  medium: "bg-amber-500/15 text-amber-200 ring-amber-500/40",
  low: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
};

const STATUS: Record<string, string> = {
  analyzing: "bg-sky-500/15 text-sky-300 ring-sky-500/40",
  awaiting_approval: "bg-fuchsia-500/15 text-fuchsia-300 ring-fuchsia-500/40",
  auto_executed: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
  approved_executed: "bg-teal-500/15 text-teal-300 ring-teal-500/40",
  monitoring: "bg-slate-500/15 text-slate-300 ring-slate-500/40",
  rejected: "bg-zinc-500/15 text-zinc-400 ring-zinc-500/40",
  resolved: "bg-indigo-500/15 text-indigo-300 ring-indigo-500/40",
};

const pill = "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset whitespace-nowrap";

export function SeverityBadge({ value }: { value: string }) {
  return <span className={`${pill} ${SEVERITY[value] ?? SEVERITY.low}`}>{value}</span>;
}

export function StatusBadge({ value }: { value: string }) {
  return <span className={`${pill} ${STATUS[value] ?? STATUS.monitoring}`}>{value.replace(/_/g, " ")}</span>;
}

export function Kpi({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone?: string }) {
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${tone ?? ""}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className="font-medium tabular-nums">{value}</div>
    </div>
  );
}
