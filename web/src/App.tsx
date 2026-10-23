import { NavLink, Route, Routes } from "react-router-dom";
import Developer from "./pages/Developer";
import DayTrading from "./pages/DayTrading";
import Home from "./pages/Home";
import Investing from "./pages/Investing";
import Learn from "./pages/Learn";
import Section from "./pages/Section";

const TABS = [
  { to: "/", label: "Home", end: true },
  { to: "/investing", label: "Investing" },
  { to: "/day-trading", label: "Day Trading" },
  { to: "/learn", label: "Learn" },
];

export default function App() {
  return (
    <div className="shell">
      <header className="topbar">
        <nav className="segmented" aria-label="Main">
          {TABS.map((t) => (
            <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => (isActive ? "seg active" : "seg")}>
              {t.label}
            </NavLink>
          ))}
        </nav>
        <NavLink to="/developer" className="gear" aria-label="Developer" title="Developer">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h0a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v0a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
          </svg>
        </NavLink>
      </header>
      <main className="page">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/investing" element={<Investing />} />
          <Route path="/investing/:section" element={<Section />} />
          <Route path="/day-trading" element={<DayTrading />} />
          <Route path="/learn" element={<Learn />} />
          <Route path="/developer/*" element={<Developer />} />
          <Route path="*" element={<Home />} />
        </Routes>
      </main>
      <footer className="footer">Simulated money only · Not financial advice</footer>
    </div>
  );
}
