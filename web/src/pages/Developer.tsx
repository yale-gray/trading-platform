import { Link, Route, Routes, useParams } from "react-router-dom";
import { useApi } from "../api";
import { LineChart } from "../components/charts";
import { pctSeries } from "../equity";
import { Card, Empty, ErrorNote, Loading, PctDelta } from "../components/ui";
import { ago, longDate } from "../format";
import type { BacktestDetail, BacktestSummary, RunnerStatus } from "../types";
import { tradeSentence } from "../copy";

function Detail() {
  const { id } = useParams();
  const r = useApi<BacktestDetail>(`/backtests/${id}`);
  if (r.error) return <ErrorNote what="this backtest" />;
  if (!r.data) return <Loading />;
  const b = r.data;
  const s = pctSeries(b.equity);
  return (
    <>
      <Link to="/developer" className="back">‹ Developer</Link>
      <h1>Backtest #{b.id}</h1>
      <Card>
        <p>Status: <strong>{b.status}</strong>{b.error ? ` · ${b.error}` : ""}</p>
        <p>Return <PctDelta value={b.total_return_pct} /> · Max drop {b.max_drawdown_pct ?? "—"}% · Sharpe {b.sharpe?.toFixed(2) ?? "—"} · Sortino {b.sortino?.toFixed(2) ?? "—"} · {b.n_trades ?? 0} trades · vs {b.benchmark_symbol}</p>
        {b.equity.length > 1 && <LineChart labels={b.equity.map((e) => e.ts)} series={[{ name: "Strategy", color: "var(--accent)", points: s.fund }, ...(s.bench.length ? [{ name: b.benchmark_symbol, color: "var(--label2)", points: s.bench, dashed: true }] : [])]} />}
      </Card>
      <h2>Trades</h2>
      <Card>
        {b.trades.length === 0 ? <Empty title="No trades" /> : <ul className="trades">{b.trades.slice(0, 50).map((t, i) => <li key={i}>{tradeSentence(t)}</li>)}</ul>}
      </Card>
      <h2>Raw</h2>
      <Card><pre className="pre">{JSON.stringify({ params: b.params, result: b.result }, null, 2)}</pre></Card>
    </>
  );
}

function List() {
  const bt = useApi<BacktestSummary[]>("/backtests");
  const runner = useApi<RunnerStatus>("/runner/status");
  return (
    <>
      <h1>Developer</h1>
      <p className="lede">Technical details. You don't need this day to day.</p>
      <h2>Runner</h2>
      <Card>
        {runner.data ? (
          <>
            <p>{runner.data.alive ? "● Running" : "● Stopped"} · last heartbeat {ago(runner.data.last_heartbeat)}</p>
            <div className="table-wrap"><table>
              <thead><tr><th>Part</th><th>Last bar</th><th>Status</th></tr></thead>
              <tbody>{runner.data.sleeves.map((s) => (
                <tr key={s.key}><td>{s.key}</td><td>{s.last_processed_bar ? longDate(s.last_processed_bar) : "—"}</td><td>{s.last_status}{s.last_error ? `: ${s.last_error}` : ""}</td></tr>
              ))}</tbody>
            </table></div>
          </>
        ) : runner.error ? <p>Runner status unavailable.</p> : <Loading />}
      </Card>
      <h2>Backtests</h2>
      <Card>
        {!bt.data ? (bt.error ? <p>Backtests unavailable.</p> : <Loading />) : bt.data.length === 0 ? <Empty title="No backtests yet" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>#</th><th>Status</th><th className="r">Return</th><th className="r">Trades</th></tr></thead>
            <tbody>{bt.data.map((b) => (
              <tr key={b.id}><td><Link to={`/developer/backtests/${b.id}`}>#{b.id}</Link></td><td>{b.status}</td><td className="r"><PctDelta value={b.total_return_pct} /></td><td className="r num">{b.n_trades ?? "—"}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>
      <h2>Older tools</h2>
      <Card>
        <ul className="plain">
          <li><a href="http://localhost:8502" target="_blank" rel="noreferrer">Old dashboard (Streamlit)</a></li>
          <li>Dev console: run <code>uv run streamlit run devconsole/app.py</code> from the project folder.</li>
          <li><a href="/docs" target="_blank" rel="noreferrer">API reference</a></li>
        </ul>
      </Card>
    </>
  );
}

export default function Developer() {
  return (
    <Routes>
      <Route index element={<List />} />
      <Route path="backtests/:id" element={<Detail />} />
    </Routes>
  );
}
