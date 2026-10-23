"""Volatility-scaled sizing (SPEC-003 section 5).

Pure functions over plain inputs: per-symbol ``vol20`` (supplied, never computed
here), a reference close and the asset class. ``Bar`` is not imported; the
engine adapts it to :class:`SymbolMarket`. All arithmetic runs in
``money_context()``; nothing depends on the ambient decimal context.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from tradebot.data.types import AssetClass
from tradebot.portfolio.money import EQUITY_QTY_QUANTUM, buy_qty, money_context

VOL_FLOOR = Decimal("0.0001")


@dataclass(frozen=True)
class SymbolMarket:
    """Plain per-symbol market input (the engine adapts ``Bar`` to this)."""

    symbol: str
    asset_class: AssetClass
    ref_price: Decimal | None
    vol20: Decimal | None = None
    tradable: bool = True


def _require_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, got {type(value).__name__}")
    return value


def vol_scaled_weights(
    weights: Mapping[str, Decimal],
    vols: Mapping[str, Decimal | None],
) -> tuple[dict[str, Decimal], tuple[str, ...]]:
    """Inverse-vol tilt: ``s_i = w_i / max(sigma_i, 1e-4)``, ``w'_i = W * s_i / sum(s)``.

    Only symbols with ``w > 0`` take part. Those whose ``vol20`` is ``None``
    (warm-up) are dropped and returned as the second element (``NO_VOL_DATA``);
    the rest renormalize to ``W`` = the sum of the *kept* original weights, so
    total exposure never grows because of a drop. Zero-weight symbols pass
    through as 0. A single kept symbol keeps its weight.
    """
    with money_context():
        dropped: list[str] = []
        kept: dict[str, Decimal] = {}
        out: dict[str, Decimal] = {}
        for sym in sorted(weights):
            w = _require_decimal(f"weights[{sym}]", weights[sym])
            if not w.is_finite() or w < 0:  # finiteness first: comparing NaN raises
                raise ValueError(f"weight for {sym} must be finite and >= 0, got {w}")
            if w == 0:
                out[sym] = Decimal(0)
                continue
            sigma = vols.get(sym)
            if sigma is None:
                dropped.append(sym)
                continue
            _require_decimal(f"vol20[{sym}]", sigma)
            if not sigma.is_finite() or sigma < 0:
                raise ValueError(f"vol20 for {sym} must be finite and >= 0, got {sigma}")
            kept[sym] = w
        if kept:
            total = sum(kept.values(), Decimal(0))
            scores = {s: w / max(vols[s] or VOL_FLOOR, VOL_FLOOR) for s, w in kept.items()}
            score_sum = sum(scores.values(), Decimal(0))
            for s, sc in scores.items():
                out[s] = total * sc / score_sum
        return dict(sorted(out.items())), tuple(dropped)


def plain_weights(weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """``w' = w`` (non-vol-scaled sleeves), validated."""
    out: dict[str, Decimal] = {}
    for sym in sorted(weights):
        w = _require_decimal(f"weights[{sym}]", weights[sym])
        if not w.is_finite() or w < 0:  # finiteness first: comparing NaN raises
            raise ValueError(f"weight for {sym} must be finite and >= 0, got {w}")
        out[sym] = w
    return out


def target_value(weight: Decimal, sizing_equity: Decimal) -> Decimal:
    """``target_value = w' * sizing_equity`` (unrounded)."""
    with money_context():
        return weight * sizing_equity


def target_quantity(
    value: Decimal,
    ref_price: Decimal,
    asset_class: AssetClass,
    equity_quantum: Decimal = EQUITY_QTY_QUANTUM,
) -> Decimal:
    """Raw desired quantity ``value / ref``, rounded DOWN onto the asset-class grid."""
    if ref_price <= 0:
        raise ValueError(f"ref_price must be > 0, got {ref_price}")
    with money_context():
        return buy_qty(max(value, Decimal(0)) / ref_price, asset_class, equity_quantum)
