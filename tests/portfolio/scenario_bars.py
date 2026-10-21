"""Hand-built synthetic daily bars for the SPEC-003 scenario tests (T13).

Every bar is written out explicitly: open, close, ``vol20`` and ``atr14``. No data
files, no indicator code, no network. High and low default to the bar's own open
and close, so they carry no information the scenarios rely on.
"""

from __future__ import annotations

from tradebot.sim.broker import Bar


def bar(
    symbol: str,
    open_: float,
    close: float,
    *,
    vol20: float | None = 0.01,
    atr14: float | None = None,
) -> Bar:
    """One synthetic daily bar for ``symbol``."""
    return Bar(
        symbol=symbol,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        vol20=vol20,
        atr14=atr14,
    )


def book(*bars: Bar) -> dict[str, Bar]:
    """The ``{symbol: Bar}`` mapping that ``process_bar`` takes for one date."""
    return {b.symbol: b for b in bars}
