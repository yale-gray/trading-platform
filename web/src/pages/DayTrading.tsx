import type { ReactNode } from "react";
import { useApi } from "../api";
import { Card, Delta, Empty, ErrorNote, Info, Loading, PctDelta, Stat, money } from "../components/ui";
import { DAY_NOTE, DAY_STRATEGIES, SURVIVORSHIP, isDayTrading, roundTripSentence } from "../copy";
import { dayKey, pct, shortDate, signedMoney } from "../format";
import type { DailyResult, EquityPoint, Portfolio, PortfolioDetail, Position, RoundTripPage } from "../types";

/** Today's change in dollars: last value minus the last value before today (or the first value today). */
export function dayChange(rows: EquityPoint[]): number | null {
  if (rows.length < 2) return null;
  const last = rows[rows.length - 1];
  const today = dayKey(last.ts);
  const prior = rows.filter((r) => dayKey(r.ts) !== today);
  const todays = rows.filter((r) => dayKey(r.ts) === today);
  const base = prior[prior.length - 1] ?? todays[0];
  if (!base || base === last) return null;
  return Number(last.equity) - Number(base.equity);
}

export default function DayTrading() {
  const ports = useApi<Portfolio[]>("/portfolios");
  const acct = (ports.data ?? []).find((p) => p.kind === "fund" && isDayTrading(p)) ?? null;
  const pid = acct?.id ?? null;
  const detail = useApi<PortfolioDetail>(pid !== null ? `/portfolios/${pid}` : null);
  const eq = useApi<EquityPoint[]>(pid !== null ? `/portfolios/${pid}/equity` : null);
  const pos = useApi<Position[]>(pid !== null ? `/portfolios/${pid}/positions` : null);
  const rt = useApi<RoundTripPage>(pid !== null ? `/portfolios/${pid}/roundtrips?limit=200` : null);
  const daily = useApi<DailyResult[]>(pid !== null ? `/portfolios/${pid}/daily` : null);

  if (ports.error) return (<><h1>Day Trading</h1><ErrorNote what="the bot" /></>);
  if (ports.loading) return (<><h1>Day Trading</h1><Loading /></>);
  if (!acct) {
    return (
      <>
        <h1>Day Trading</h1>
        <p className="lede">A practice account that buys and sells within the same day.</p>
        <Card>
          <Empty title="Day-trading account not created yet.">
            Create it with <code>uv run tradebot portfolio create --preset daytrading</code>.
          </Empty>
        </Card>
        <p className="muted small">Simulated money only · Not financial advice</p>
      </>
    );
  }

  const rows = eq.data ?? [];
  const chg = dayChange(rows);
  // Round trips are newest exit first; keep the ones that closed today in local time.
  const todayKey = dayKey(new Date());
  const closedToday = (rt.data?.items ?? []).filter((t) => dayKey(t.exit_ts) === todayKey);
  const wins = closedToday.filter((t) => Number(t.pnl) > 0).length;
  const pnlToday = closedToday.reduce((sum, t) => sum + Number(t.pnl), 0);
  const winRateToday = closedToday.length ? Math.round((wins / closedToday.length) * 100) : null;
  const recentDays = [...(daily.data ?? [])].slice(-10).reverse();
  const openNow = pos.data?.length ?? 0;
  const caveat = detail.data?.caveats?.[0] ?? SURVIVORSHIP;
  const settled = detail.data?.settled_cash;
  const unsettled = detail.data?.unsettled_cash;

  return (
    <>
      <h1>Day Trading</h1>
      <p className="lede">Its own {money(acct.initial_cash, true)} of simulated money, traded within each day.</p>

      <Card className="hero">
        <div className="label">Account value</div>
        <div className="big num">{money(acct.equity)}</div>
        <div>
          {chg !== null ? <Delta value={chg} label="today" /> : <span className="muted small">Today's change appears once there are prices from today.</span>}
        </div>
        <div className="stats">
          <Stat label="Total return" term="return" value={<PctDelta value={acct.total_return_pct} />} note="Since the account started." />
          <Stat label="Worst drop" term="drawdown" value={<Delta value={acct.max_drawdown_pct === 0 ? 0 : -Math.abs(acct.max_drawdown_pct)} text={pct(-Math.abs(acct.max_drawdown_pct), false)} />} note="The biggest fall from a high point." />
        </div>
      </Card>

      <Card className="cash-strip">
        <p className="cash-line">
          <span>{settled !== undefined && unsettled !== undefined
            ? `Ready to trade ${money(settled)} · Settling ${money(unsettled)} (available tomorrow)`
            : "Cash figures appear after the first bar."}</span>{" "}
          <Info term="settlement" />
        </p>
      </Card>

      <h2>Today</h2>
      <Card>
        <div className="stats">
          <Stat
            label="Round trips today"
            value={closedToday.length}
            note={closedToday.length ? "Buy-then-sell pairs closed today." : "None closed yet today."}
          />
          <Stat
            label="Win rate today"
            value={winRateToday === null ? "—" : `${winRateToday}%`}
            note={winRateToday === null ? "Shows once a round trip closes." : `${wins} of ${closedToday.length} made money.`}
          />
          <Stat
            label="P&L today"
            value={closedToday.length ? <Delta value={pnlToday} /> : "—"}
            note="After fees, from closed round trips."
          />
          <Stat label="Open positions right now" term="intraday" value={openNow} note="0 overnight" />
        </div>
      </Card>

      <h2>Strategies</h2>
      <div className="grid">
        {Object.entries(DAY_STRATEGIES).map(([key, s]) => {
          const sl = detail.data?.sleeves.find((x) => x.key === key);
          return (
            <Card key={key}>
              <h3>{s.title}</h3>
              <div className="label">What it does</div>
              <p>{s.does}</p>
              <div className="mid num">{sl ? money(sl.equity) : "—"}</div>
              <div>{sl ? <PctDelta value={sl.return_pct} label="since start" /> : <span className="muted small">Not reported yet.</span>}</div>
            </Card>
          );
        })}
      </div>

      <h2>Today's trades</h2>
      <Card>
        {rt.error ? (
          <p className="muted">Couldn't load today's trades.</p>
        ) : closedToday.length === 0 ? (
          <Empty title="No round trips closed today">When the bot buys and then sells a stock, each pair will be listed here in plain English.</Empty>
        ) : (
          <ul className="trades">
            {closedToday.map((t, i) => <li key={i}>{roundTripSentence(t)}</li>)}
          </ul>
        )}
      </Card>

      <h2>Daily results</h2>
      <Card>
        {daily.error ? (
          <p className="muted">Couldn't load daily results.</p>
        ) : recentDays.length === 0 ? (
          <Empty title="No sessions yet">Each trading day's result will appear here once the account has a full session.</Empty>
        ) : (
          <ul className="trades daily">
            {recentDays.map((d) => <DailyRow key={d.date} d={d} />)}
          </ul>
        )}
      </Card>

      <Card className="calm-note">
        <p>{DAY_NOTE}</p>
      </Card>

      <h2>The strategies in short</h2>
      <Card>
        {Object.values(DAY_STRATEGIES).map((s) => (
          <p key={s.title}><strong>{s.title}.</strong> {s.strategy}</p>
        ))}
        <p className="note"><strong>Heads up:</strong> past results use {caveat}. Companies that later failed or were dropped from the index aren't included, which can make history look better than it was.</p>
      </Card>

      <p className="muted small">Simulated money only · Not financial advice</p>
    </>
  );
}

/** One session: date, P&L in dollars and percent, and how it compared with the benchmark. */
function DailyRow({ d }: { d: DailyResult }) {
  const pnl = Number(d.pnl);
  const trips = d.n_roundtrips === 1 ? "1 round trip" : `${d.n_roundtrips} round trips`;
  let versus: ReactNode;
  if (d.benchmark_pnl_pct === null) {
    versus = <span className="muted">No benchmark price that day.</span>;
  } else {
    const gap = d.pnl_pct - d.benchmark_pnl_pct;
    const text = Math.abs(gap) < 0.05
      ? "matched the benchmark"
      : `${gap > 0 ? "beat" : "trailed"} the benchmark by ${Math.abs(gap).toFixed(1)} points`;
    versus = <Delta value={gap} text={text} label={`(benchmark ${pct(d.benchmark_pnl_pct)})`} />;
  }
  return (
    <li>
      <strong>{shortDate(`${d.date}T12:00:00`)}</strong>{" · "}
      <Delta value={pnl} text={`${signedMoney(pnl)} (${pct(d.pnl_pct)})`} />{" · "}
      <span className="muted">{trips}</span>
      <div className="small">{versus}</div>
    </li>
  );
}
