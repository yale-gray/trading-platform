import { Card } from "../components/ui";
import { GLOSSARY } from "../glossary";
import { SECTIONS } from "../copy";

export default function Learn() {
  return (
    <>
      <h1>Learn</h1>
      <p className="lede">A few minutes to understand what the bot is doing, in plain English.</p>

      <h2>How the bot decides</h2>
      <Card>
        <p>Once a day, after the US stock market closes, the bot looks at the day's prices. It never peeks at tomorrow: a decision made from today's prices is only carried out at tomorrow's open.</p>
        <p>Each part of the account follows its own simple rule, and every result is compared with just buying and holding SPY. If the bot can't beat that, it isn't worth the complexity.</p>
        <p>All the money is pretend. Nothing here can place a real trade.</p>
      </Card>

      <h2>The three strategies</h2>
      <div className="grid three">
        {Object.entries(SECTIONS).map(([k, s]) => (
          <Card key={k}><h3>{s.title}</h3><p>{s.strategy}</p></Card>
        ))}
      </div>

      <h2>Safety rules</h2>
      <Card>
        <ul className="plain">
          <li><strong>25% cap:</strong> no single holding can grow past a quarter of the account.</li>
          <li><strong>30% cap:</strong> no single kind of asset can pass 30%.</li>
          <li><strong>Stop-loss:</strong> a holding that falls too far is sold to limit the damage.</li>
          <li><strong>Kill-switch pause:</strong> if the account falls 20% from its peak, the bot stops buying for about 20 trading days. Selling still works. Afterwards it resumes and measures from the new level.</li>
        </ul>
      </Card>

      <h2>Glossary</h2>
      <Card>
        <dl className="glossary">
          {Object.entries(GLOSSARY).map(([k, g]) => (
            <div key={k}><dt>{g.term}</dt><dd>{g.text}</dd></div>
          ))}
        </dl>
      </Card>
    </>
  );
}
