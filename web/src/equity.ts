import type { EquityPoint } from "./types";
import { toPctSeries } from "./components/charts";

/** Display-only delta between the last two snapshots (today's change). */
export function todayChange(eq: EquityPoint[]): { amount: number; pct: number } | null {
  if (eq.length < 2) return null;
  const a = Number(eq[eq.length - 2].equity), b = Number(eq[eq.length - 1].equity);
  if (!a) return null;
  return { amount: b - a, pct: (b / a - 1) * 100 };
}

export const RANGES = ["1M", "3M", "YTD", "1Y", "All"] as const;
export type Range = (typeof RANGES)[number];

export function filterRange(eq: EquityPoint[], range: Range, now: Date = new Date()): EquityPoint[] {
  if (range === "All" || eq.length === 0) return eq;
  const cut = new Date(now);
  if (range === "1M") cut.setUTCMonth(cut.getUTCMonth() - 1);
  else if (range === "3M") cut.setUTCMonth(cut.getUTCMonth() - 3);
  else if (range === "1Y") cut.setUTCFullYear(cut.getUTCFullYear() - 1);
  else { cut.setUTCMonth(0, 1); cut.setUTCHours(0, 0, 0, 0); }
  const out = eq.filter((p) => new Date(p.ts + (p.ts.endsWith("Z") ? "" : "Z")) >= cut);
  return out.length ? out : eq.slice(-1);
}

export function pctSeries(eq: EquityPoint[]): { fund: number[]; bench: number[] } {
  return {
    fund: toPctSeries(eq.map((p) => Number(p.equity))),
    bench: toPctSeries(eq.map((p) => (p.benchmark_equity === null ? null : Number(p.benchmark_equity)))),
  };
}
