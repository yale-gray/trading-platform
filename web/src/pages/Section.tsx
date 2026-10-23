import { Link, useParams, useSearchParams } from "react-router-dom";
import { useApi } from "../api";
import { Card, Delta, Empty, ErrorNote, Info, Loading, PctDelta, money } from "../components/ui";
import { SECTIONS, SURVIVORSHIP, tradeSentence } from "../copy";
import { qty } from "../format";
import type { Page, PortfolioDetail, Position, Trade } from "../types";

export default function Section() {
  const { section = "" } = useParams();
  const [qs] = useSearchParams();
  const pid = qs.get("p");
  const meta = SECTIONS[section];
  const d = useApi<PortfolioDetail>(pid ? `/portfolios/${pid}` : null);
  const pos = useApi<Position[]>(pid ? `/portfolios/${pid}/positions` : null);
  const tr = useApi<Page<Trade>>(pid ? `/portfolios/${pid}/trades?sleeve=${section}&limit=20` : null);

  if (!pid || !meta) return (<><h1>Not found</h1><Link to="/investing">‹ Back to Investing</Link></>);
  if (d.error) return (<><Link to="/investing">‹ Investing</Link><h1>{meta.title}</h1><ErrorNote what="the bot" /></>);
  const sl = d.data?.sleeves.find((s) => s.key === section);
  const holdings = (pos.data ?? []).filter((p) => p.sleeve_key === section);
  const caveat = d.data?.caveats?.[0] ?? SURVIVORSHIP;

  return (
    <>
      <Link to="/investing" className="back">‹ Investing</Link>
      <h1>{meta.title}</h1>
      <p className="lede">{meta.does}</p>
      {!sl ? <Loading /> : (
        <Card className="hero">
          <div className="big num">{money(sl.equity)}</div>
          <PctDelta value={sl.return_pct} label="since start" />
          <p className="muted small">Target share of the account: {Math.round(sl.target_weight_pct)}% · now {sl.actual_weight_pct.toFixed(0)}%</p>
        </Card>
      )}

      <h2>Holdings</h2>
      <Card>
        {holdings.length === 0 ? (
          <Empty title="Nothing held right now">This part of the account is sitting in cash. That is normal when the strategy sees no good opportunity, or before the first purchase.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Name</th><th className="r">Shares</th><th className="r">Value</th><th className="r">Gain</th></tr></thead>
              <tbody>
                {holdings.map((h) => (
                  <tr key={h.symbol}>
                    <td>{h.symbol}</td>
                    <td className="r num">{qty(h.qty)}</td>
                    <td className="r num">{money(h.market_value)}</td>
                    <td className="r"><Delta value={Number(h.unrealized_pnl)} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2>Recent trades</h2>
      <Card>
        {(tr.data?.items.length ?? 0) === 0 ? (
          <Empty title="No trades yet">When the bot buys or sells in this section, each trade will be listed here in plain English.</Empty>
        ) : (
          <ul className="trades">
            {tr.data!.items.map((t, i) => <li key={i}>{tradeSentence(t)}</li>)}
          </ul>
        )}
      </Card>

      <h2>The strategy</h2>
      <Card>
        <p>{meta.strategy}</p>
        <p className="muted small">Safety rules: no single holding above 25% of the account, no single kind of asset above 30% <Info term="cap" />.</p>
        {section === "stocks" && <p className="note"><strong>Heads up:</strong> past results use {caveat}. Companies that later failed or were dropped from the index aren't included, which can make history look better than it was <Info term="survivorship" />.</p>}
      </Card>
    </>
  );
}
