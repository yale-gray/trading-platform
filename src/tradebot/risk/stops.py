"""Close-based ATR trailing stop and stop-out lock (SPEC-003 §7, I13).

Pure Decimal logic: the caller supplies the close, ``atr14`` (never computed
here) and the multiplier. State is JSON-serializable with Decimals as text so
``EngineState`` can persist it. ``StopLevels`` holds exactly the two values the
``Portfolio`` stop overlay stores (``high_water_close``, ``stop_level``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from tradebot.portfolio.money import from_text, money_context, q_price, to_text
from tradebot.sim.orders import OrderOrigin

STOP_ORIGIN = OrderOrigin.STOP


@dataclass(frozen=True)
class StopLevels:
    """Stop overlay for one open position (what ``Portfolio.set_stop_fields`` stores)."""

    high_water_close: Decimal
    stop_level: Decimal | None


@dataclass(frozen=True)
class StopUpdate:
    """Result of one close: new levels and whether a full-position STOP exit is due."""

    levels: StopLevels
    triggered: bool
    origin: OrderOrigin = STOP_ORIGIN


def update_stop(
    prev: StopLevels | None,
    close: Decimal,
    atr14: Decimal | None,
    mult: Decimal,
) -> StopUpdate:
    """Advance the trailing stop for one close.

    ``prev=None`` means the fill bar: ``high_water_close = close``. Otherwise
    ``high_water_close = max(prev, close)``. ``candidate = hwc - mult * atr14``;
    ``stop_level = max(prev_stop, candidate)`` floored at 0 (ratchets up only).
    ``atr14`` of None or <= 0 leaves the level unchanged but it is still tested.
    Triggers when ``close <= stop_level``.
    """
    with money_context():
        hwc = close if prev is None else max(prev.high_water_close, close)
        stop = None if prev is None else prev.stop_level
        if atr14 is not None and atr14 > 0:
            candidate = q_price(hwc - mult * atr14, ROUND_HALF_EVEN)
            candidate = max(candidate, Decimal(0))
            stop = candidate if stop is None else max(stop, candidate)
        triggered = stop is not None and close <= stop
    return StopUpdate(StopLevels(hwc, stop), triggered)


class StopBook:
    """Per-sleeve stop levels and the stop-out lock (``stopped_out``)."""

    def __init__(self) -> None:
        self._levels: dict[str, dict[str, StopLevels]] = {}
        self._stopped_out: dict[str, set[str]] = {}

    def levels(self, sleeve_id: str, symbol: str) -> StopLevels | None:
        """Current levels for an open position, or None."""
        return self._levels.get(sleeve_id, {}).get(symbol)

    def on_close(
        self,
        sleeve_id: str,
        symbol: str,
        close: Decimal,
        atr14: Decimal | None,
        mult: Decimal,
    ) -> StopUpdate:
        """Update (or start, on the fill bar) the stop and test it.

        On a trigger the symbol enters the sleeve's stop-out lock.
        """
        res = update_stop(self.levels(sleeve_id, symbol), close, atr14, mult)
        self._levels.setdefault(sleeve_id, {})[symbol] = res.levels
        if res.triggered:
            self._stopped_out.setdefault(sleeve_id, set()).add(symbol)
        return res

    def clear(self, sleeve_id: str, symbol: str) -> None:
        """Drop levels when the position returns to flat (lock is kept)."""
        self._levels.get(sleeve_id, {}).pop(symbol, None)

    def is_locked(self, sleeve_id: str, symbol: str) -> bool:
        """True while the symbol is stopped out and not yet re-armed."""
        return symbol in self._stopped_out.get(sleeve_id, set())

    def note_target(self, sleeve_id: str, symbol: str, target_is_zero: bool) -> None:
        """Record the strategy's target; zero/omitted re-arms a locked symbol."""
        if target_is_zero:
            self._stopped_out.get(sleeve_id, set()).discard(symbol)

    def to_state(self) -> dict[str, Any]:
        """JSON-serializable state (Decimals as text)."""
        return {
            "levels": {
                s: {
                    sym: {
                        "high_water_close": to_text(lv.high_water_close),
                        "stop_level": None if lv.stop_level is None else to_text(lv.stop_level),
                    }
                    for sym, lv in sorted(d.items())
                }
                for s, d in sorted(self._levels.items())
                if d
            },
            "stopped_out": {s: sorted(v) for s, v in sorted(self._stopped_out.items()) if v},
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> StopBook:
        """Inverse of :meth:`to_state`."""
        book = cls()
        for s, d in state.get("levels", {}).items():
            for sym, lv in d.items():
                sl = lv["stop_level"]
                book._levels.setdefault(s, {})[sym] = StopLevels(
                    from_text(lv["high_water_close"]), None if sl is None else from_text(sl)
                )
        for s, syms in state.get("stopped_out", {}).items():
            book._stopped_out[s] = set(syms)
        return book
