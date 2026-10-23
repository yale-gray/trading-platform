import { clockTime, longDate, money, pct, qty, shortDate, signedMoney } from "./format";
import type { ExitReason, Portfolio, RoundTrip, Trade } from "./types";

const CRYPTO = /-|\//;

/** "Bought 12 shares of SPY at $587.20 on Oct 22". */
export function tradeSentence(t: Pick<Trade, "side" | "qty" | "symbol" | "price" | "ts" | "sleeve_key">): string {
  const buy = t.side.toUpperCase().startsWith("B");
  const crypto = t.sleeve_key === "crypto" || CRYPTO.test(t.symbol);
  const n = Number(t.qty);
  const what = crypto ? `${qty(t.qty)} ${t.symbol}` : `${qty(t.qty)} ${n === 1 ? "share" : "shares"} of ${t.symbol}`;
  return `${buy ? "Bought" : "Sold"} ${what} at ${money(t.price)} on ${shortDate(t.ts)}`;
}

export const SECTIONS: Record<string, { title: string; icon: string; does: string; strategy: string }> = {
  index: {
    title: "Index funds",
    icon: "▦",
    does: "Owns the whole market in two big funds, SPY and QQQ, and mostly just holds on.",
    strategy:
      "Holds SPY and QQQ half and half, and tidies the mix once a month. It is deliberately simple: four fancier ideas were tested and none beat just holding SPY.",
  },
  stocks: {
    title: "Stocks",
    icon: "◍",
    does: "Picks S&P 500 companies whose prices have been rising strongest, and sits in cash when nothing looks good.",
    strategy:
      "Once a month the bot ranks S&P 500 companies by how strongly their price has been rising (momentum) and holds the best few. A slot with no good candidate stays in cash instead of being filled with something weaker.",
  },
  crypto: {
    title: "Crypto",
    icon: "◈",
    does: "Holds crypto only while its price trend is pointing up, and steps aside into cash when it turns down.",
    strategy:
      "A simple trend rule: when a coin's price is trending up the bot holds it, and when the trend turns down it sells and waits. Crypto swings a lot, so it gets the smallest share of the account.",
  },
};

export const SURVIVORSHIP = "current S&P 500 constituents (survivorship-biased)";

export const KILL_SWITCH_BLURB = (until: Date | null, dd: number | null, threshold: number): string => {
  const drop = `${(dd ?? threshold).toFixed(0)}%`;
  return until
    ? `Buying paused until about ${longDate(until)} after a ${drop} drop. Selling still works.`
    : `Buying is paused after a ${drop} drop. Selling still works.`;
};

/** The day-trading account is the fund named daytrading (the backend default is day_trading). */
export const isDayTrading = (p: Pick<Portfolio, "name">): boolean => p.name.replace(/_/g, "").toLowerCase() === "daytrading";

/** "Bought 2.4 shares of NVDA at $182.10 at 10:45 am". Date-only timestamps fall back to the day. */
export function dayTradeSentence(t: Pick<Trade, "side" | "qty" | "symbol" | "price" | "ts">): string {
  const buy = t.side.toUpperCase().startsWith("B");
  const n = Number(t.qty);
  const what = `${qty(t.qty)} ${n === 1 ? "share" : "shares"} of ${t.symbol}`;
  const clock = clockTime(t.ts);
  const when = clock ? `at ${clock}` : `on ${shortDate(t.ts)}`;
  return `${buy ? "Bought" : "Sold"} ${what} at ${money(t.price)} ${when}`;
}

export const DAY_STRATEGIES: Record<string, { title: string; does: string; strategy: string }> = {
  dip: {
    title: "Dip buyer",
    does: "Buys S&P 500 stocks that drop sharply below their recent average while the short-term trend is still up.",
    strategy:
      "Watches 15-minute bars. When a stock dips well below its own short average but its short trend is still up, it buys. It sells when the price snaps back to the average, on a trailing stop, or at the close. At most five positions at once.",
  },
  breakout: {
    title: "Breakout",
    does: "Buys S&P 500 stocks that push above the high of their first hour of trading on heavier-than-usual volume.",
    strategy:
      "Marks each stock's first-hour high (the opening range). When a stock closes above that high on above-average volume it buys, and sells on a trailing stop, if it falls back below the level, or at the close. At most five positions at once.",
  },
};

export const DAY_NOTE = "Buys and sells within the same day. Everything is sold before the 4 pm close.";

/** Exit reasons in plain words. Keys match the API's ExitReason values. */
export const EXIT_REASON_WORDS: Record<ExitReason, string> = {
  signal: "exit rule met",
  stop: "safety stop",
  rebalance: "rebalanced",
  manual: "sold by hand",
  flatten: "sold at the close",
  flatten_no_bar: "sold at the close (no last price, extra cost)",
};

/** "Bought NVDA at 10:45 am, sold at 11:30 am · +$3.12 (+0.4%) · exit rule met". */
export function roundTripSentence(t: Pick<RoundTrip, "symbol" | "entry_ts" | "exit_ts" | "pnl" | "pnl_pct" | "exit_reason">): string {
  const at = (ts: string) => clockTime(ts) ?? shortDate(ts);
  const reason = EXIT_REASON_WORDS[t.exit_reason] ?? t.exit_reason;
  return `Bought ${t.symbol} at ${at(t.entry_ts)}, sold at ${at(t.exit_ts)} · ${signedMoney(Number(t.pnl))} (${pct(t.pnl_pct)}) · ${reason}`;
}
