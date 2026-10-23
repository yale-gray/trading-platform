import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { post, useApi } from "../api";
import { LineChart } from "../components/charts";
import { Card, Empty, ErrorNote, Loading, PctDelta, Stat, Delta, money } from "../components/ui";
import { KILL_SWITCH_BLURB, SECTIONS } from "../copy";
import { RANGES, filterRange, pctSeries, type Range } from "../equity";
import { pauseEnd, pct } from "../format";
import type { EquityPoint, KillSwitch, Portfolio, PortfolioDetail } from "../types";
import { label } from "./Home";

export function KillSwitchNote({ pid, ks, onAck }: { pid: number; ks: KillSwitch; onAck: () => void }) {
  const [busy, setBusy] = useState(false);
  if (!ks.tripped) return null;
  const until = pauseEnd(ks);
  const acked = ks.acknowledged_at !== null;
  return (
    <Card className="calm-note" role="status">
      <strong>Safety pause</strong>
      <p>{KILL_SWITCH_BLURB(until, ks.drawdown_pct, ks.threshold_pct)}</p>
      {acked ? <p className="muted small">You've acknowledged this. The pause lifts by itself.</p> : (
        <button className="btn" disabled={busy} onClick={async () => {
          setBusy(true);
          try { await post(`/portfolios/${pid}/kill-switch/ack`, { note: "Acknowledged in the web app" }); onAck(); } finally { setBusy(false); }
        }}>Acknowledge</button>
      )}
      <p className="muted small">Ending the pause early is a terminal command, not a button here.</p>
    </Card>
  );
}

export default function Investing() {
  const ports = useApi<Portfolio[]>("/portfolios");
  const funds = (ports.data ?? []).filter((p) => p.kind === "fund");
  const [sel, setSel] = useState<number | null>(null);
  const pid = sel ?? funds[0]?.id ?? null;
  const detail = useApi<PortfolioDetail>(pid ? `/portfolios/${pid}` : null);
  const eq = useApi<EquityPoint[]>(pid ? `/portfolios/${pid}/equity` : null);
  const [range, setRange] = useState<Range>("3M");
  useEffect(() => { document.title = "Investing · Tradebot"; }, []);

  if (ports.error) return (<><h1>Investing</h1><ErrorNote what="the bot" /></>);
  if (ports.loading) return (<><h1>Investing</h1><Loading /></>);
  if (!funds.length || !pid) return (<><h1>Investing</h1><Card><Empty title="No investing account yet">Once the bot is set up, your account will appear here.</Empty></Card></>);
  const d = detail.data;
  const rows = filterRange(eq.data ?? [], range);
  const s = pctSeries(rows);
  const worst = d ? d.max_drawdown_pct : 0;

  return (
    <>
      <h1>Investing</h1>
      {funds.length > 1 && (
        <select aria-label="Account" className="select" value={pid} onChange={(e) => setSel(Number(e.target.value))}>
          {funds.map((f) => <option key={f.id} value={f.id}>{label(f.name)}</option>)}
        </select>
      )}
      {d && (
        <>
          <Card className="hero">
            <div className="label">{label(d.name)}</div>
            <div className="big num">{money(d.equity)}</div>
            <div className="stats">
              <Stat label="Value" value={money(d.equity, true)} note={`Started with ${money(d.initial_cash, true)} of simulated money.`} />
              <Stat label="Total return" term="return" value={<PctDelta value={d.total_return_pct} />} note="How much the account has grown since it started." />
              <Stat label="Worst drop" term="drawdown" value={<Delta value={worst === 0 ? 0 : -Math.abs(worst)} text={pct(-Math.abs(worst), false)} />} note="The biggest fall from a high point, a measure of bumpiness." />
            </div>
          </Card>

          <KillSwitchNote pid={d.id} ks={d.kill_switch} onAck={detail.reload} />

          <Card>
            <div className="chart-head">
              <h3>You vs SPY</h3>
              <div className="segmented small-seg" role="group" aria-label="Range">
                {RANGES.map((r) => (
                  <button key={r} type="button" className={r === range ? "seg active" : "seg"} aria-pressed={r === range} onClick={() => setRange(r)}>{r}</button>
                ))}
              </div>
            </div>
            {rows.length < 2 ? (
              <Empty title="The chart is waiting for data">The bot records a point after each trading day. Come back after a few closes.</Empty>
            ) : (
              <LineChart labels={rows.map((r) => r.ts)} series={[
                { name: "You", color: "var(--accent)", points: s.fund },
                ...(s.bench.length ? [{ name: "SPY", color: "var(--label2)", points: s.bench, dashed: true }] : []),
              ]} />
            )}
            <p className="muted small">Both lines start at 0% on the first day shown, so you can compare fairly.</p>
          </Card>

          <h2>Where the money is</h2>
          <div className="grid three">
            {d.sleeves.map((sl) => {
              const meta = SECTIONS[sl.key];
              return (
                <Link key={sl.key} to={`/investing/${sl.key}?p=${d.id}`} className="card link section-card">
                  <div className="label">{meta?.title ?? sl.key} · {Math.round(sl.target_weight_pct)}%</div>
                  <div className="mid num">{money(sl.equity)}</div>
                  <PctDelta value={sl.return_pct} label="return" />
                  <p className="muted small"><b>What it does:</b> {meta?.does ?? sl.strategy_name ?? ""}</p>
                  <span className="chev" aria-hidden="true">›</span>
                </Link>
              );
            })}
          </div>
        </>
      )}
      {detail.loading && !d && <Loading />}
    </>
  );
}
