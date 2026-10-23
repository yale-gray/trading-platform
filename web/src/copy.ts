import { longDate, money, qty, shortDate } from "./format";
import type { Trade } from "./types";

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
