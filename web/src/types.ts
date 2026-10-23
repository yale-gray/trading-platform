/** API shapes (money arrives as decimal strings). */
export type Portfolio = {
  id: number; name: string; kind: "fund" | "benchmark"; status: string;
  equity: string; initial_cash: string; total_return_pct: number; max_drawdown_pct: number;
  kill_switch_tripped: boolean; last_processed_bar: string | null; started_at: string;
};
export type Sleeve = {
  key: string; strategy_name: string | null; target_weight_pct: number;
  actual_weight_pct: number; equity: string; return_pct: number; n_positions: number;
};
export type KillSwitch = {
  tripped: boolean; tripped_at_bar: string | null; drawdown_pct: number | null;
  threshold_pct: number; acknowledged_at: string | null; note: string | null; pause_ends_after_bar?: string | null;
};
export type PortfolioDetail = Portfolio & {
  sleeves: Sleeve[]; kill_switch: KillSwitch; benchmarks: Portfolio[]; caveats?: string[];
  cash: string; unsettled_cash: string; settled_cash: string; as_of?: string | null;
};
export type ExitReason = "signal" | "stop" | "rebalance" | "manual" | "flatten" | "flatten_no_bar";
/** One completed buy-then-sell (GET /portfolios/{id}/roundtrips). Timestamps are naive UTC. */
export type RoundTrip = {
  sleeve_key: string; symbol: string; entry_ts: string; exit_ts: string; qty: string;
  entry_price: string; exit_price: string; fees: string; pnl: string; pnl_pct: number; exit_reason: ExitReason;
};
export type RoundTripPage = {
  items: RoundTrip[]; total: number; limit: number; win_rate: number | null; total_pnl: string;
};
/** One session of the account versus its benchmark (GET /portfolios/{id}/daily, oldest first). */
export type DailyResult = {
  date: string; start_equity: string; end_equity: string; pnl: string; pnl_pct: number;
  n_roundtrips: number; benchmark_pnl_pct: number | null;
};
export type EquityPoint = {
  ts: string; equity: string; drawdown_pct: number;
  benchmark_equity: string | null; benchmark_missing: boolean;
};
export type Position = {
  sleeve_key: string; symbol: string; qty: string; avg_cost: string; mark_price: string;
  market_value: string; unrealized_pnl: string; weight_pct: number;
};
export type Trade = {
  ts: string; sleeve_key: string; symbol: string; side: string; qty: string; price: string;
  fee: string; slippage: string | null; reason: string;
};
export type Page<T> = { items: T[]; total: number; limit: number; offset: number };
export type RunnerStatus = {
  alive: boolean; last_heartbeat: string | null; next_run_after?: string | null;
  sleeves: { key: string; last_processed_bar: string | null; last_status: string; last_error: string | null }[];
};
export type BacktestSummary = {
  id: number; strategy_id: number | null; status: string; started_at: string | null;
  finished_at: string | null; total_return_pct: number | null; sharpe: number | null;
  sortino: number | null; max_drawdown_pct: number | null; n_trades: number | null;
  benchmark_symbol: string;
};
export type BacktestDetail = BacktestSummary & {
  params: Record<string, unknown>; result: Record<string, unknown> | null;
  equity: EquityPoint[]; trades: Trade[]; error: string | null;
};
