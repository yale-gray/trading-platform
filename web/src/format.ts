/** Display formatting only. Money stays a decimal string; we never do money math beyond display deltas. */
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export function money(v: string | number | null | undefined, whole = false): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return (whole ? usd0 : usd).format(n);
}

/** "+$12.30" / "-$4.00" (display only). */
export function signedMoney(n: number): string {
  const s = usd.format(Math.abs(n));
  return n > 0 ? `+${s}` : n < 0 ? `-${s}` : s;
}

export function pct(n: number | null | undefined, signed = true): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const s = `${Math.abs(n).toFixed(1)}%`;
  if (!signed) return n < 0 ? `-${s}` : s;
  return n > 0.05 ? `+${s}` : n < -0.05 ? `-${s}` : "0.0%";
}

export function qty(v: string | number): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(n);
}

/** The API sends naive UTC timestamps; treat them as UTC. */
export function parseTs(ts: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : `${ts}Z`);
}

export function shortDate(ts: string | Date): string {
  const d = typeof ts === "string" ? parseTs(ts) : ts;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

export function longDate(ts: string | Date): string {
  const d = typeof ts === "string" ? parseTs(ts) : ts;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

/** "5 minutes ago", "yesterday"... */
export function ago(ts: string | null, now: Date = new Date()): string {
  if (!ts) return "never";
  const s = Math.max(0, (now.getTime() - parseTs(ts).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} minutes ago`;
  if (s < 86400) return `${Math.round(s / 3600)} hours ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

/** Add n weekdays (holidays ignored, so the result is approximate). */
export function addWeekdays(ts: string, n: number): Date {
  const d = parseTs(ts);
  let left = n;
  while (left > 0) {
    d.setUTCDate(d.getUTCDate() + 1);
    const w = d.getUTCDay();
    if (w !== 0 && w !== 6) left -= 1;
  }
  return d;
}

/** When buying resumes: the exact bar from the API, else a rough 20-weekday estimate. */
export function pauseEnd(ks: { tripped_at_bar: string | null; pause_ends_after_bar?: string | null }): Date | null {
  if (ks.pause_ends_after_bar) return parseTs(ks.pause_ends_after_bar);
  return ks.tripped_at_bar ? addWeekdays(ks.tripped_at_bar, 20) : null;
}

/** "Fri, Oct 23, 4:30 PM" in the viewer's local time; null when unknown or unparsable. */
export function nextRunLocal(ts: string | null | undefined): string | null {
  if (!ts) return null;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString("en-US", { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
