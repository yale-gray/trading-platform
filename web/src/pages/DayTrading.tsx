import { Card, Info } from "../components/ui";

export default function DayTrading() {
  return (
    <>
      <h1>Day Trading</h1>
      <p className="lede">Coming soon. This account isn't trading yet.</p>
      <Card>
        <h3>What it will do</h3>
        <ul className="plain">
          <li>Its own separate $10,000 of simulated money.</li>
          <li>Trades S&amp;P 500 stocks, looking at 15-minute price bars.</li>
          <li>Never holds anything overnight: everything is sold before the market closes (<Info term="daytrading" />).</li>
          <li>Follows the same rules a real $10,000 account would face, so results stay realistic.</li>
        </ul>
        <div className="note">
          <strong>Waiting for:</strong> a free Alpaca market-data key. It is only used to read prices, never to place trades. Once it's added, this page will fill in.
        </div>
      </Card>
    </>
  );
}
