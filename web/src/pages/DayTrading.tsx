import { useApi } from "../api";
import { Card, Delta, Empty, ErrorNote, Loading, PctDelta, Stat, money } from "../components/ui";
import { DAY_NOTE, DAY_STRATEGIES, SURVIVORSHIP, dayTradeSentence, isDayTrading } from "../copy";
import { dayKey, pct } from "../format";
import type { EquityPoint, Page, Portfolio, PortfolioDetail, Position, Trade } from "../types";

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
  const tr = useApi<Page<Trade>>(pid !== null ? `/portfolios/${pid}/trades?limit=200` : null);

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
  const items = tr.data?.items ?? [];
  const todayKey = dayKey(new Date());
  const tradesToday = items.filter((t) => dayKey(t.ts) === todayKey);
  const openNow = pos.data?.length ?? 0;
  const caveat = detail.data?.caveats?.[0] ?? SURVIVORSHIP;

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

      <h2>Today</h2>
      <Card>
        <div className="stats">
          <Stat label="Trades today" value={tradesToday.length} note="Buys and sells so far today." />
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

      <h2>Recent trades</h2>
      <Card>
        {items.length === 0 ? (
          <Empty title="No trades yet">When the bot buys or sells, each trade will be listed here in plain English.</Empty>
        ) : (
          <ul className="trades">
            {items.slice(0, 10).map((t, i) => <li key={i}>{dayTradeSentence(t)}</li>)}
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
