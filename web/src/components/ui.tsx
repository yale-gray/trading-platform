import { useId, useState, type ReactNode } from "react";
import { GLOSSARY } from "../glossary";
import { money, pct, signedMoney } from "../format";

/** ⓘ with a glossary definition (hover, focus or tap). */
export function Info({ term }: { term: keyof typeof GLOSSARY | string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const g = GLOSSARY[term];
  if (!g) return null;
  return (
    <span className="info" onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        className="info-btn"
        aria-label={`What is ${g.term}?`}
        aria-describedby={open ? id : undefined}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        onMouseEnter={() => setOpen(true)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => e.key === "Escape" && setOpen(false)}
      >
        ⓘ
      </button>
      {open && (
        <span role="tooltip" id={id} className="tip">
          <strong>{g.term}.</strong> {g.text}
        </span>
      )}
    </span>
  );
}

/** Gain/loss: always an arrow plus text, never color alone. */
export function Delta({ value, text, label }: { value: number; text?: string; label?: string }) {
  const cls = value > 0 ? "gain" : value < 0 ? "loss" : "flat";
  const arrow = value > 0 ? "▲" : value < 0 ? "▼" : "–";
  return (
    <span className={`delta ${cls}`}>
      <span aria-hidden="true">{arrow}</span> {text ?? signedMoney(value)}
      {label ? <span className="muted"> {label}</span> : null}
    </span>
  );
}

export function PctDelta({ value, label }: { value: number | null; label?: string }) {
  if (value === null) return <span className="muted">—</span>;
  return <Delta value={value} text={pct(value)} label={label} />;
}

export function Card({ children, className = "", ...rest }: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLElement>) {
  return (
    <section className={`card ${className}`} {...rest}>
      {children}
    </section>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty" role="status">
      <strong>{title}</strong>
      {children ? <p>{children}</p> : null}
    </div>
  );
}

export function Loading() {
  return <p className="muted" role="status">Loading…</p>;
}

export function ErrorNote({ what }: { what: string }) {
  return (
    <Card>
      <strong>Can't reach {what} right now.</strong>
      <p className="muted">Make sure the bot's API is running (<code>uv run tradebot api serve</code>), then refresh.</p>
    </Card>
  );
}

export function Stat({ label, term, value, note }: { label: string; term?: string; value: ReactNode; note: string }) {
  return (
    <div className="stat">
      <div className="label">
        {label} {term ? <Info term={term} /> : null}
      </div>
      <div className="stat-value num">{value}</div>
      <p className="muted small">{note}</p>
    </div>
  );
}

export { money };
