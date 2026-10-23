"""Speed benchmark: 500-symbol x 1-year 15m backtest of the day-trading preset (ADR-0001 A21).

Not collected by pytest (``tests/manual`` is ignored). Run from the repo root::

    uv run python tests/manual/bench_intraday.py 500 12          # current engine
    uv run python tests/manual/bench_intraday.py 500 12 legacy   # emulates the pre-A21 hot paths
    uv run python tests/manual/bench_intraday.py 100 2 prof      # cProfile top 35

``legacy`` switches off lean marking, makes every ``Ledger.state_view`` read copy the folded state
(as the old ``ledger.state`` calls did) and restores the full-``_last_close`` crypto scan in
snapshots; the ledger it produces is identical to the current engine's.
"""

import cProfile
import pstats
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from datetime import date

from support.intraday_synth import make_intraday_bars

from tradebot.backtest.engine import BacktestConfig, SleeveSpec, run_backtest
from tradebot.runner.bootstrap import daytrading_definition

N = int(sys.argv[1]) if len(sys.argv) > 1 else 500
months = int(sys.argv[2]) if len(sys.argv) > 2 else 12
legacy = len(sys.argv) > 3 and sys.argv[3] == "legacy"
prof = len(sys.argv) > 3 and sys.argv[3] == "prof"
defn = daytrading_definition(date(2023, 1, 3))
syms = sorted(defn.required_symbols())[:N]
sleeves = [
    SleeveSpec(sleeve_id=sl.sleeve_id, strategy=sl.strategy.model_copy(update={"universe": syms}))
    for sl in defn.sleeves
]
end = (
    date(2023, 1, 3 + 0).replace(month=min(12, months), day=28)
    if months < 12
    else date(2023, 12, 29)
)
t = time.time()
bars = make_intraday_bars(syms, date(2022, 12, 1), end, seed=3)
print("gen bars", round(time.time() - t, 1), "s", sum(len(v) for v in bars.values()), "bars")
cfg = BacktestConfig(sleeves=sleeves, start=date(2023, 1, 3), end=end, engine=defn.engine)
if legacy:
    from tradebot.backtest import driver as drv
    from tradebot.portfolio import engine as eng_mod
    from tradebot.portfolio.ledger import Ledger

    Ledger.state_view = property(lambda self: self.state)  # old behaviour: copy the folded state
    orig_make = eng_mod.PortfolioEngine._make_snapshot
    from tradebot.data.types import AssetClass

    def old_make(self, asof):
        crypto = [
            s for s in sorted(self._last_close) if self._asset_class_of(s) is AssetClass.CRYPTO
        ]
        return self._portfolio.snapshot(
            asof,
            self._last_close,
            peak_equity=self._ks.peak_equity,
            killed=self._ks.killed,
            crypto_symbols=crypto,
        )

    eng_mod.PortfolioEngine._make_snapshot = old_make
    orig_init = drv.BarDriver.__init__

    def init(self, *a, **k):
        k["lean"] = False
        orig_init(self, *a, **k)

    drv.BarDriver.__init__ = init
t = time.time()
if prof:
    pr = cProfile.Profile()
    pr.enable()
res = run_backtest(cfg, bars)
if prof:
    pr.disable()
    pstats.Stats(pr).sort_stats("cumulative").print_stats(35)
print(
    "backtest",
    round(time.time() - t, 1),
    "s;",
    len(res.equity),
    "bars;",
    len(res.trades),
    "trades; final",
    res.equity.iloc[-1],
)
