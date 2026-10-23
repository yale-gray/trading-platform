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
  it("Day Trading explains the placeholder", () => {
    render(<DayTrading />);
    expect(screen.getByText(/Alpaca/)).toBeInTheDocument();
  });
});
