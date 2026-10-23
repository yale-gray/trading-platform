import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import Home from "./Home";
import Investing from "./Investing";
import DayTrading from "./DayTrading";
import { Delta, Info } from "../components/ui";
import { roundTripSentence } from "../copy";

const fund = { id: 2, name: "smart_investing", kind: "fund", status: "active", equity: "10000.00", initial_cash: "10000.00", total_return_pct: 0, max_drawdown_pct: 0, kill_switch_tripped: false, last_processed_bar: null, started_at: "2026-10-21T00:00:00" };
const detail = { ...fund, sleeves: [{ key: "index", strategy_name: "index_hold", target_weight_pct: 50, actual_weight_pct: 50, equity: "5000.00", return_pct: 0, n_positions: 0 }], kill_switch: { tripped: false, tripped_at_bar: null, drawdown_pct: null, threshold_pct: 20, acknowledged_at: null, note: null }, benchmarks: [] };

function mockApi(overrides: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    "/api/v1/portfolios": [fund],
    "/api/v1/portfolios/2": detail,
    "/api/v1/portfolios/2/equity": [],
    "/api/v1/runner/status": { alive: false, last_heartbeat: null, sleeves: [] },
    ...overrides,
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok: url in routes, status: 404, json: async () => routes[url] })));
}
afterEach(() => vi.unstubAllGlobals());

describe("components", () => {
  it("Delta pairs color with an arrow and text", () => {
    render(<Delta value={-5} />);
    expect(screen.getByText(/-\$5\.00/)).toBeInTheDocument();
    expect(screen.getByText("▼")).toBeInTheDocument();
  });
  it("Info shows the glossary definition", () => {
    render(<Info term="drawdown" />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("tooltip")).toHaveTextContent("biggest fall");
  });
});

describe("pages", () => {
  it("Home shows the accounts, stopped bot and empty-state copy", async () => {
    mockApi();
    render(<MemoryRouter><Home /></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText("Smart Investing").length).toBeGreaterThan(0));
    expect(screen.getByText("Day Trading")).toBeInTheDocument();
    expect(await screen.findByText(/Stopped/)).toBeInTheDocument();
    expect(screen.getByText(/isn't running/)).toBeInTheDocument();
    expect(screen.getAllByText(/after the next daily close/).length).toBeGreaterThan(0);
  });
  it("Home shows the next run in local time when the API gives it", async () => {
    mockApi({ "/api/v1/runner/status": { alive: true, last_heartbeat: null, next_run_after: "2026-10-23T20:30:00Z", sleeves: [] } });
    render(<MemoryRouter><Home /></MemoryRouter>);
    expect(await screen.findByText(/Next run: \w{3}, Oct 2[34]/)).toBeInTheDocument();
  });
  it("Investing shows an empty chart state and section cards", async () => {
    mockApi();
    render(<MemoryRouter><Investing /></MemoryRouter>);
    expect(await screen.findByText("The chart is waiting for data")).toBeInTheDocument();
    expect(await screen.findByText(/Index funds · 50%/)).toBeInTheDocument();
  });
});

describe("Day Trading page", () => {
  const dayFund = { id: 3, name: "day_trading", kind: "fund", status: "active", equity: "10120.00", initial_cash: "10000.00", total_return_pct: 1.2, max_drawdown_pct: 0.8, kill_switch_tripped: false, last_processed_bar: "2026-10-22T19:45:00Z", started_at: "2026-10-20T00:00:00" };
  const dayDetail = {
    ...dayFund,
    sleeves: [
      { key: "dip", strategy_name: "stocks_dt_dip", target_weight_pct: 50, actual_weight_pct: 30, equity: "5060.00", return_pct: 1.2, n_positions: 2 },
      { key: "breakout", strategy_name: "stocks_dt_breakout", target_weight_pct: 50, actual_weight_pct: 20, equity: "5060.00", return_pct: 1.2, n_positions: 1 },
    ],
    kill_switch: { tripped: false, tripped_at_bar: null, drawdown_pct: null, threshold_pct: 20, acknowledged_at: null, note: null },
    benchmarks: [],
    caveats: ["current S&P 500 constituents (survivorship-biased)"],
    cash: "10120.00",
    unsettled_cash: "1500.00",
    settled_cash: "8620.00",
  };
  // Yesterday's close, then two of today's points (full UTC datetimes).
  const dayEquity = [
    { ts: "2026-10-21T20:00:00", equity: "10000.00", drawdown_pct: 0, benchmark_equity: null, benchmark_missing: false },
    { ts: "2026-10-22T13:30:00Z", equity: "10050.00", drawdown_pct: 0, benchmark_equity: null, benchmark_missing: false },
    { ts: "2026-10-22T19:45:00Z", equity: "10120.00", drawdown_pct: 0, benchmark_equity: null, benchmark_missing: false },
  ];
  // Round trips that closed right now (so they count as "today" in the viewer's local time).
  const nowIso = new Date().toISOString();
  const hourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  const winner = { sleeve_key: "dip", symbol: "NVDA", entry_ts: hourAgo, exit_ts: nowIso, qty: "2.4", entry_price: "182.10", exit_price: "183.40", fees: "0.10", pnl: "3.12", pnl_pct: 0.4, exit_reason: "signal" };
  const loser = { sleeve_key: "breakout", symbol: "AMD", entry_ts: hourAgo, exit_ts: nowIso, qty: "5", entry_price: "160.00", exit_price: "158.00", fees: "0.10", pnl: "-10.10", pnl_pct: -1.3, exit_reason: "stop" };
  const roundtrips = { items: [winner, loser], total: 2, limit: 200, win_rate: 0.5, total_pnl: "-6.98" };
  const yesterday = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  const daily = [
    { date: "2026-10-20", start_equity: "10000.00", end_equity: "10010.00", pnl: "10.00", pnl_pct: 0.1, n_roundtrips: 1, benchmark_pnl_pct: 0.3 },
    { date: yesterday, start_equity: "10010.00", end_equity: "9960.00", pnl: "-50.00", pnl_pct: -0.5, n_roundtrips: 3, benchmark_pnl_pct: null },
  ];
  const dayRoutes = {
    "/api/v1/portfolios": [dayFund],
    "/api/v1/portfolios/3": dayDetail,
    "/api/v1/portfolios/3/equity": dayEquity,
    "/api/v1/portfolios/3/positions": [],
    "/api/v1/portfolios/3/roundtrips?limit=200": roundtrips,
    "/api/v1/portfolios/3/daily": daily,
  };

  it("shows the empty state when no day-trading account exists", async () => {
    mockApi();
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText("Day-trading account not created yet.")).toBeInTheDocument();
    expect(screen.getByText("uv run tradebot portfolio create --preset daytrading", { selector: "code" })).toBeInTheDocument();
  });

  it("shows the populated page with strategies, today's change and the caveat", async () => {
    mockApi(dayRoutes);
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText("Account value")).toBeInTheDocument();
    expect(screen.getByText("+$120.00")).toBeInTheDocument(); // today: 10120 minus yesterday's 10000
    expect(screen.getByText("Dip buyer")).toBeInTheDocument();
    expect(screen.getByText("Breakout")).toBeInTheDocument();
    expect(screen.getAllByText("What it does")).toHaveLength(2);
    expect(screen.getByText(/survivorship-biased/)).toBeInTheDocument();
    expect(screen.getByText(/Buys and sells within the same day/)).toBeInTheDocument();
    expect(screen.getAllByText(/Simulated money only · Not financial advice/).length).toBeGreaterThan(0);
  });

  it("shows the cash strip with the settling amount and a settlement tooltip", async () => {
    mockApi(dayRoutes);
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText("Ready to trade $8,620.00 · Settling $1,500.00 (available tomorrow)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "What is Settlement?" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("business day");
  });

  it("lists today's round trips in plain English with a summary", async () => {
    mockApi(dayRoutes);
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText(/^Bought NVDA at .+, sold at .+ · \+\$3\.12 \(\+0\.4%\) · exit rule met$/)).toBeInTheDocument();
    expect(screen.getByText(/^Bought AMD at .+, sold at .+ · -\$10\.10 \(-1\.3%\) · safety stop$/)).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument(); // win rate today: 1 of 2
    expect(screen.getByText("-$6.98")).toBeInTheDocument(); // P&L today: 3.12 - 10.10
  });

  it("shows the daily results with arrows and the benchmark comparison", async () => {
    mockApi(dayRoutes);
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText("Daily results")).toBeInTheDocument();
    // Newest first, last 10 sessions only.
    const items = Array.from(screen.getByRole("heading", { name: "Daily results" }).nextElementSibling!.querySelectorAll("li"));
    expect(items[0]).toHaveTextContent("▼");
    expect(items[0]).toHaveTextContent("-$50.00 (-0.5%)");
    expect(items[0]).toHaveTextContent("3 round trips");
    expect(items[0]).toHaveTextContent("No benchmark price that day.");
    expect(items[1]).toHaveTextContent("▲");
    expect(items[1]).toHaveTextContent("+$10.00 (+0.1%)");
    expect(items[1]).toHaveTextContent("trailed the benchmark by 0.2 points");
    expect(items[1]).toHaveTextContent("(benchmark +0.3%)");
  });

  it("never shows an overnight position", async () => {
    mockApi(dayRoutes);
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText("0 overnight")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/[1-9]\d* overnight/);
    expect(document.body.textContent).not.toMatch(/held overnight/i);
  });

  it("shows empty states when nothing closed today and no sessions exist", async () => {
    mockApi({
      ...dayRoutes,
      "/api/v1/portfolios/3/roundtrips?limit=200": { items: [], total: 0, limit: 200, win_rate: null, total_pnl: "0.00" },
      "/api/v1/portfolios/3/daily": [],
    });
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText("No round trips closed today")).toBeInTheDocument();
    expect(screen.getByText("No sessions yet")).toBeInTheDocument();
    expect(screen.getByText("Shows once a round trip closes.")).toBeInTheDocument();
  });

  it("shows a cash fallback when the account has no cash figures yet", async () => {
    const { settled_cash, unsettled_cash, cash, ...noCash } = dayDetail;
    void settled_cash; void unsettled_cash; void cash;
    mockApi({ ...dayRoutes, "/api/v1/portfolios/3": noCash });
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText("Cash figures appear after the first bar.")).toBeInTheDocument();
  });
});

describe("round-trip sentences", () => {
  const base = { symbol: "NVDA", entry_ts: "2026-10-22T14:45:00", exit_ts: "2026-10-22T15:30:00", pnl: "3.12", pnl_pct: 0.4 };
  it.each([
    ["signal", "exit rule met"],
    ["stop", "safety stop"],
    ["flatten", "sold at the close"],
    ["flatten_no_bar", "sold at the close (no last price, extra cost)"],
  ] as const)("describes exit reason %s in words", (reason, words) => {
    expect(roundTripSentence({ ...base, exit_reason: reason })).toMatch(new RegExp(`^Bought NVDA at .+, sold at .+ · \\+\\$3\\.12 \\(\\+0\\.4%\\) · ${words.replace(/[()]/g, "\\$&")}$`));
  });
  it("writes a losing trade with a minus sign", () => {
    expect(roundTripSentence({ ...base, pnl: "-2.5", pnl_pct: -0.2, exit_reason: "stop" })).toContain("-$2.50 (-0.2%)");
  });
});
