import { Link } from "react-router-dom";
import { useApi } from "../api";
import { Sparkline } from "../components/charts";
import { Card, Delta, Empty, ErrorNote, Info, Loading, PctDelta, money } from "../components/ui";
import { pctSeries, todayChange } from "../equity";
import { ago, longDate, nextRunLocal, pauseEnd } from "../format";
import { isDayTrading } from "../copy";
import type { EquityPoint, Portfolio, PortfolioDetail, RunnerStatus } from "../types";

export type Attention = { key: string; text: string; to?: string };

export function attentionItems(
  funds: PortfolioDetail[], runner: RunnerStatus | null, equity: Record<number, EquityPoint[]>,
): Attention[] {
  const out: Attention[] = [];
  for (const f of funds) {
    if (f.kill_switch.tripped) {
      const end = pauseEnd(f.kill_switch);
      const until = end ? longDate(end) : null;
      out.push({ key: `ks${f.id}`, to: "/investing", text: `Buying is paused on ${label(f.name)}${until ? ` until about ${until}` : ""}. Selling still works.` });
    }
    const eq = equity[f.id] ?? [];
    if (eq.some((p) => p.benchmark_missing)) out.push({ key: `dm${f.id}`, text: `Some comparison data is missing for ${label(f.name)}. Charts may have gaps.` });
  }
  if (runner && !runner.alive) out.push({ key: "runner", to: "/developer", text: "The bot isn't running, so nothing new will be traded or recorded." });
  return out;
}

export const label = (name: string) => (name === "smart_investing" ? "Smart Investing" : name.replace(/_/g, " "));

function AccountCard({ p }: { p: Portfolio }) {
  const eq = useApi<EquityPoint[]>(`/portfolios/${p.id}/equity`);
  const rows = eq.data ?? [];
  const chg = todayChange(rows);
  const s = pctSeries(rows);
  return (
    <Link to="/investing" className="card account link">
      <div className="label">{label(p.name)}</div>
      <div className="big num">{money(p.equity)}</div>
      <div>
        {chg ? <Delta value={chg.amount} label="today" /> : <span className="muted small">Today's change appears after the next daily close.</span>}
      </div>
      <Sparkline fund={s.fund} bench={s.bench} />
      <div className="muted small">
        <span className="swatch fund" /> You <span className="swatch bench" /> SPY · Since start: <PctDelta value={p.total_return_pct} />
      </div>
    </Link>
  );
}

export default function Home() {
  const ports = useApi<Portfolio[]>("/portfolios");
  const runner = useApi<RunnerStatus>("/runner/status");
  const funds = (ports.data ?? []).filter((p) => p.kind === "fund" && !isDayTrading(p));
  const dayAcct = (ports.data ?? []).find((p) => p.kind === "fund" && isDayTrading(p));
  const fund = funds[0];
  const detail = useApi<PortfolioDetail>(fund ? `/portfolios/${fund.id}` : null);
  const eq = useApi<EquityPoint[]>(fund ? `/portfolios/${fund.id}/equity` : null);

  if (ports.error) return (<><h1>Home</h1><ErrorNote what="the bot" /></>);
  const items = attentionItems(detail.data ? [detail.data] : [], runner.data, fund && eq.data ? { [fund.id]: eq.data } : {});
  const r = runner.data;

  return (
    <>
      <h1>Home</h1>
      <p className="lede">Two practice accounts, each started with $10,000 of pretend money.</p>
      {ports.loading ? <Loading /> : (
        <div className="grid">
          {funds.length === 0 && <Card><Empty title="No accounts yet">Once the bot has been set up, your accounts will show up here.</Empty></Card>}
          {funds.map((p) => <AccountCard key={p.id} p={p} />)}
          <Link to="/day-trading" className="card account link">
            <div className="label">Day Trading</div>
            {dayAcct ? (
              <>
                <div className="big num">{money(dayAcct.equity)}</div>
                <div className="muted small">Since start: <PctDelta value={dayAcct.total_return_pct} /></div>
              </>
            ) : (
              <>
                <div className="big num">{money("10000")}</div>
                <div className="muted small">Not created yet. Buys and sells within the same day.</div>
              </>
            )}
          </Link>
        </div>
      )}

      <h2>Bot status</h2>
      <Card className="row">
        <div>
          <div className={r?.alive ? "status on" : "status off"}><span aria-hidden="true">●</span> {r ? (r.alive ? "Running" : "Stopped") : "Checking…"}</div>
          <p className="muted small">
            Last run: {r ? ago(r.last_heartbeat) : "—"} · Next run: {nextRunLocal(r?.next_run_after) ?? "after the next US market close (about 4:30 pm New York time), once per trading day."}
          </p>
        </div>
      </Card>

      <h2>Needs your attention</h2>
      {items.length === 0 ? (
        <Card><span className="calm">✓ All clear. Nothing needs you right now.</span></Card>
      ) : (
        <Card>
          <ul className="attn">
            {items.map((a) => (
              <li key={a.key}>{a.to ? <Link to={a.to}>{a.text}</Link> : a.text}</li>
            ))}
          </ul>
        </Card>
      )}
      <p className="muted small">Return is shown against a simple yardstick, the <Info term="benchmark" /> (holding SPY).</p>
    </>
  );
}
