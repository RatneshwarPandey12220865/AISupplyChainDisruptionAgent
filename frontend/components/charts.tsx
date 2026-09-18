import { usd } from "@/lib/api";

/** Monte Carlo loss distribution with percentile markers. */
export function LossHistogram({ sim }: { sim: any }) {
  const counts: number[] = sim?.histogram?.counts ?? [];
  const edges: number[] = sim?.histogram?.edges ?? [];
  if (!counts.length) return null;
  const W = 560, H = 170, pad = 24;
  const max = Math.max(...counts);
  const lo = edges[0], hi = edges[edges.length - 1];
  const x = (v: number) => pad + ((v - lo) / Math.max(hi - lo, 1e-9)) * (W - 2 * pad);
  const bw = (W - 2 * pad) / counts.length;
  const markers = [
    { k: "p15", label: "P15", color: "#fbbf24" },
    { k: "p50", label: "P50", color: "#e2e8f0" },
    { k: "p85", label: "P85", color: "#fb7185" },
  ];
  return (
    <svg viewBox={`0 0 ${W} ${H + 20}`} className="w-full">
      {counts.map((c, i) => {
        const h = (c / max) * (H - 30);
        return <rect key={i} x={pad + i * bw + 1} y={H - h} width={bw - 2} height={h} rx={2} fill="#38bdf8" opacity={0.75} />;
      })}
      <line x1={pad} x2={W - pad} y1={H} y2={H} stroke="#1f2b45" />
      {markers.map((m) => (
        <g key={m.k}>
          <line x1={x(sim[m.k])} x2={x(sim[m.k])} y1={12} y2={H} stroke={m.color} strokeDasharray="4 3" />
          <text x={x(sim[m.k])} y={10} fill={m.color} fontSize="10" textAnchor="middle">{m.label}</text>
        </g>
      ))}
      <text x={pad} y={H + 16} fill="#8a9bbd" fontSize="10">{usd(lo)}</text>
      <text x={W - pad} y={H + 16} fill="#8a9bbd" fontSize="10" textAnchor="end">{usd(hi)}</text>
    </svg>
  );
}

/** Expected loss as a function of how long we wait to engage the mitigation. */
export function UrgencyCurve({ curve, windowHours }: { curve: { hours: number; expected_loss: number }[]; windowHours: number }) {
  if (!curve?.length) return null;
  const W = 560, H = 140, pad = 34;
  const maxH = Math.max(...curve.map((c) => c.hours));
  const vals = curve.map((c) => c.expected_loss);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const x = (h: number) => pad + (h / Math.max(maxH, 1)) * (W - 2 * pad);
  const y = (v: number) => H - 18 - ((v - lo) / Math.max(hi - lo, 1)) * (H - 40);
  const path = curve.map((c, i) => `${i ? "L" : "M"}${x(c.hours)},${y(c.expected_loss)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <rect x={pad} y={8} width={x(windowHours) - pad} height={H - 26} fill="#10b981" opacity={0.08} />
      <path d={path} fill="none" stroke="#fb7185" strokeWidth={2} />
      {curve.map((c) => (
        <g key={c.hours}>
          <circle cx={x(c.hours)} cy={y(c.expected_loss)} r={3} fill="#fb7185" />
          <text x={x(c.hours)} y={H - 4} fill="#8a9bbd" fontSize="10" textAnchor="middle">{c.hours}h</text>
          <text x={x(c.hours)} y={y(c.expected_loss) - 7} fill="#cbd5e1" fontSize="9" textAnchor="middle">
            {usd(c.expected_loss)}
          </text>
        </g>
      ))}
    </svg>
  );
}

/** Equirectangular supply network: suppliers (sized by reliability, red when at risk) and US DCs. */
export function SupplyMap({ suppliers, shipments, signals }: { suppliers: any[]; shipments: any[]; signals: any[] }) {
  const W = 640, H = 300;
  const px = (lon: number) => ((lon + 180) / 360) * W;
  const py = (lat: number) => ((75 - lat) / 135) * H;
  const DC: Record<string, [number, number]> = { DAL: [32.78, -96.8], NWK: [40.73, -74.17], RNO: [39.53, -119.81] };
  const riskyRegions = new Set(signals.filter((s) => s.active).map((s) => s.region));
  const byName = Object.fromEntries(suppliers.map((s) => [s.name, s]));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full rounded-lg bg-[#0d1628]">
      {[-120, -60, 0, 60, 120].map((lon) => <line key={lon} x1={px(lon)} x2={px(lon)} y1={0} y2={H} stroke="#15203a" />)}
      {[-30, 0, 30, 60].map((lat) => <line key={lat} y1={py(lat)} y2={py(lat)} x1={0} x2={W} stroke="#15203a" />)}
      {shipments.filter((s) => s.status !== "delivered").map((sh) => {
        const sup = byName[sh.supplier];
        const dc = DC[sh.warehouse];
        if (!sup || !dc) return null;
        const x1 = px(sup.lon), y1 = py(sup.lat), x2 = px(dc[1]), y2 = py(dc[0]);
        return (
          <path key={sh.code} d={`M${x1},${y1} Q${(x1 + x2) / 2},${Math.min(y1, y2) - 40} ${x2},${y2}`} fill="none"
            stroke={sh.delay_days > 0 ? "#fb7185" : "#38bdf8"} strokeOpacity={0.5} strokeDasharray={sh.delay_days > 0 ? "5 4" : ""} />
        );
      })}
      {Object.entries(DC).map(([code, [lat, lon]]) => (
        <g key={code}>
          <rect x={px(lon) - 4} y={py(lat) - 4} width={8} height={8} fill="#e2e8f0" />
          <text x={px(lon)} y={py(lat) + 16} fill="#cbd5e1" fontSize="10" textAnchor="middle">{code}</text>
        </g>
      ))}
      {suppliers.map((s) => {
        const risky = riskyRegions.has(s.region) || s.status !== "operational";
        return (
          <g key={s.id}>
            {risky && <circle cx={px(s.lon)} cy={py(s.lat)} r={14} fill="#fb7185" opacity={0.18} />}
            <circle cx={px(s.lon)} cy={py(s.lat)} r={3 + 5 * s.reliability_score} fill={risky ? "#fb7185" : "#34d399"} />
            <text x={px(s.lon) + 9} y={py(s.lat) + 4} fill="#cbd5e1" fontSize="9">{s.name.split(" ")[0]}</text>
          </g>
        );
      })}
    </svg>
  );
}
