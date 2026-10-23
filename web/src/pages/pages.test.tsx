import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import Home from "./Home";
import Investing from "./Investing";
import DayTrading from "./DayTrading";
import { Delta, Info } from "../components/ui";

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
  };
  // Yesterday's close, then two of today's points (full UTC datetimes).
  const dayEquity = [
    { ts: "2026-10-21T20:00:00", equity: "10000.00", drawdown_pct: 0, benchmark_equity: null, benchmark_missing: false },
    { ts: "2026-10-22T13:30:00Z", equity: "10050.00", drawdown_pct: 0, benchmark_equity: null, benchmark_missing: false },
    { ts: "2026-10-22T19:45:00Z", equity: "10120.00", drawdown_pct: 0, benchmark_equity: null, benchmark_missing: false },
  ];
  const trade = { ts: "2026-10-22T14:45:00Z", sleeve_key: "dip", symbol: "NVDA", side: "BUY", qty: "2.4", price: "182.10", fee: "0", slippage: "0.01", reason: "dip" };
  const dayRoutes = {
    "/api/v1/portfolios": [dayFund],
    "/api/v1/portfolios/3": dayDetail,
    "/api/v1/portfolios/3/equity": dayEquity,
    "/api/v1/portfolios/3/positions": [],
    "/api/v1/portfolios/3/trades?limit=200": { items: [trade], total: 1, limit: 200, offset: 0 },
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

  it("writes each trade as a plain sentence with its local time", async () => {
    mockApi(dayRoutes);
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText(/^Bought 2\.4 shares of NVDA at \$182\.10 at \d{1,2}:\d{2} (am|pm)$/)).toBeInTheDocument();
  });

  it("never shows an overnight position", async () => {
    mockApi(dayRoutes);
    render(<MemoryRouter><DayTrading /></MemoryRouter>);
    expect(await screen.findByText("0 overnight")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/[1-9]\d* overnight/);
    expect(document.body.textContent).not.toMatch(/held overnight/i);
  });
});
