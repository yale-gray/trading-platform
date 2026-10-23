import { describe, expect, it } from "vitest";
import { ago, money, pct, signedMoney, addWeekdays, pauseEnd, nextRunLocal } from "./format";
import { tradeSentence } from "./copy";
import { filterRange, todayChange } from "./equity";

describe("format", () => {
  it("formats money from decimal strings", () => {
    expect(money("10000.00")).toBe("$10,000.00");
    expect(money("10000.4", true)).toBe("$10,000");
    expect(money(null)).toBe("—");
    expect(signedMoney(-4)).toBe("-$4.00");
  });
  it("formats percents with sign", () => {
    expect(pct(3.21)).toBe("+3.2%");
    expect(pct(-1.26)).toBe("-1.3%");
    expect(pct(0)).toBe("0.0%");
    expect(pct(null)).toBe("—");
  });
  it("describes time in plain English", () => {
    const now = new Date("2026-10-22T12:00:00Z");
    expect(ago(null, now)).toBe("never");
    expect(ago("2026-10-22T11:00:00", now)).toBe("1 hours ago");
    expect(ago("2026-10-21T11:00:00", now)).toBe("yesterday");
  });
  it("skips weekends", () => {
    expect(addWeekdays("2026-10-23T00:00:00", 1).getUTCDay()).toBe(1);
  });
  it("prefers the exact pause end over the weekday estimate", () => {
    const ks = { tripped_at_bar: "2026-10-01T00:00:00", pause_ends_after_bar: "2026-10-29" };
    expect(pauseEnd(ks)?.toISOString().slice(0, 10)).toBe("2026-10-29");
    expect(pauseEnd({ tripped_at_bar: "2026-10-01T00:00:00" })?.toISOString().slice(0, 10)).toBe("2026-10-29");
    expect(pauseEnd({ tripped_at_bar: null, pause_ends_after_bar: null })).toBeNull();
  });
  it("formats the next run or falls back to null", () => {
    expect(nextRunLocal(null)).toBeNull();
    expect(nextRunLocal("2026-10-23T20:30:00Z")).toMatch(/Oct 2[34]/);
  });
});

describe("tradeSentence", () => {
  it("reads like English", () => {
    expect(tradeSentence({ side: "BUY", qty: "12", symbol: "SPY", price: "587.20", ts: "2026-10-22T00:00:00", sleeve_key: "index" }))
      .toBe("Bought 12 shares of SPY at $587.20 on Oct 22");
    expect(tradeSentence({ side: "SELL", qty: "1", symbol: "AAPL", price: "200", ts: "2026-10-22T00:00:00", sleeve_key: "stocks" }))
      .toBe("Sold 1 share of AAPL at $200.00 on Oct 22");
    expect(tradeSentence({ side: "BUY", qty: "0.0123", symbol: "BTC-USD", price: "60000", ts: "2026-10-22T00:00:00", sleeve_key: "crypto" }))
      .toBe("Bought 0.0123 BTC-USD at $60,000.00 on Oct 22");
  });
});

describe("equity helpers", () => {
  const pt = (ts: string, e: string) => ({ ts, equity: e, drawdown_pct: 0, benchmark_equity: null, benchmark_missing: false });
  it("computes today's change and handles empty", () => {
    expect(todayChange([])).toBeNull();
    const c = todayChange([pt("2026-10-21T00:00:00", "100"), pt("2026-10-22T00:00:00", "101")]);
    expect(c?.amount).toBeCloseTo(1);
    expect(c?.pct).toBeCloseTo(1);
  });
  it("filters ranges", () => {
    const eq = [pt("2025-01-01T00:00:00", "1"), pt("2026-10-01T00:00:00", "2"), pt("2026-10-20T00:00:00", "3")];
    expect(filterRange(eq, "1M", new Date("2026-10-22T00:00:00Z"))).toHaveLength(2);
    expect(filterRange(eq, "All")).toHaveLength(3);
  });
});
