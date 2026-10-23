import { useMemo, useState } from "react";
import { pct, shortDate } from "../format";

export type Series = { name: string; color: string; points: number[]; dashed?: boolean };

/** Normalise a money series to "% change since the first point". */
export function toPctSeries(values: (number | null)[]): number[] {
  const first = values.find((v): v is number => v !== null && v !== 0);
  if (first === undefined) return [];
  return values.map((v) => (v === null ? NaN : (v / first - 1) * 100));
}

function path(pts: number[], x: (i: number) => number, y: (v: number) => number): string {
  let d = "";
  let pen = false;
  pts.forEach((v, i) => {
    if (Number.isNaN(v)) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

/** Tiny trend line for a card (fund solid, benchmark dashed). */
export function Sparkline({ fund, bench }: { fund: number[]; bench?: number[] }) {
  const W = 160, H = 40;
  const all = [...fund, ...(bench ?? [])].filter((v) => !Number.isNaN(v));
  if (fund.length < 2 || all.length < 2) return <div className="spark-empty muted small">Chart appears after a few days</div>;
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const x = (i: number) => (i / (fund.length - 1)) * W;
  const y = (v: number) => H - 3 - ((v - lo) / span) * (H - 6);
  return (
    <svg className="spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Trend compared with the benchmark">
      {bench && bench.length > 1 && <path d={path(bench, (i) => (i / (bench.length - 1)) * W, y)} className="line-bench" />}
      <path d={path(fund, x, y)} className="line-fund" />
    </svg>
  );
}

/** Responsive line chart in % with hover/touch readout. */
export function LineChart({ labels, series }: { labels: string[]; series: Series[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 640, H = 240, L = 44, R = 8, T = 10, B = 22;
  const n = labels.length;
  const { lo, hi } = useMemo(() => {
    const v = series.flatMap((s) => s.points).filter((p) => !Number.isNaN(p));
    const lo = Math.min(0, ...v), hi = Math.max(0, ...v);
    const pad = (hi - lo || 1) * 0.08;
    return { lo: lo - pad, hi: hi + pad };
  }, [series]);
  if (n < 2) return null;
  const x = (i: number) => L + (i / (n - 1)) * (W - L - R);
  const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const ticks = [lo, (lo + hi) / 2, hi].map((t) => Math.round(t));
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    setHover(Math.max(0, Math.min(n - 1, Math.round(((px - L) / (W - L - R)) * (n - 1)))));
  };
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Chart: you compared with SPY" onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} className={t === 0 ? "axis-zero" : "grid"} />
            <text x={L - 6} y={y(t) + 4} textAnchor="end" className="tick">{t}%</text>
          </g>
        ))}
        <text x={L} y={H - 4} className="tick">{shortDate(labels[0])}</text>
        <text x={W - R} y={H - 4} textAnchor="end" className="tick">{shortDate(labels[n - 1])}</text>
        {series.map((s) => (
          <path key={s.name} d={path(s.points, x, y)} fill="none" stroke={s.color} strokeWidth={2} strokeDasharray={s.dashed ? "5 4" : undefined} strokeLinejoin="round" strokeLinecap="round" />
        ))}
        {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} className="grid" />}
      </svg>
      <div className="legend" aria-live="polite">
        {series.map((s) => (
          <span key={s.name} className="legend-item">
            <i style={{ background: s.color }} /> {s.name}
            {hover !== null && !Number.isNaN(s.points[hover]) ? <b className="num"> {pct(s.points[hover])}</b> : null}
          </span>
        ))}
        {hover !== null && <span className="muted small">{shortDate(labels[hover])}</span>}
      </div>
    </div>
  );
}
