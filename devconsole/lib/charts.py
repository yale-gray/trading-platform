"""Plotly figure builders for the dev console. No streamlit imports here.

Every indicator value comes from ``tradebot.strategy.indicators.INDICATORS``.
Colours are semi-transparent or neutral so the charts read in light and dark themes.
"""

from collections.abc import Mapping, Sequence

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from tradebot.strategy.indicators import INDICATORS

UP_COLOR = "#2ca02c"
DOWN_COLOR = "#d62728"
GRID_COLOR = "rgba(128, 128, 128, 0.25)"
HLINE_COLOR = "rgba(128, 128, 128, 0.6)"

PRICE_INDICATORS = frozenset({"sma", "ema", "bbands"})
OSCILLATOR_INDICATORS = frozenset({"rsi", "atr", "macd", "vol", "ret"})

_FLOAT_PARAMS = frozenset({"k"})


def _style(fig: go.Figure) -> go.Figure:
    fig.update_layout(
        paper_bgcolor="rgba(0, 0, 0, 0)",
        plot_bgcolor="rgba(0, 0, 0, 0)",
        margin={"l": 60, "r": 20, "t": 50, "b": 40},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=GRID_COLOR)
    fig.update_yaxes(gridcolor=GRID_COLOR)
    return fig


def candlestick_with_volume(
    bars: pd.DataFrame, symbol: str, overlays: Mapping[str, pd.Series]
) -> go.Figure:
    """Candlesticks (row 1, 70% height) with price overlays, and volume (row 2).

    Equities hide weekend gaps via rangebreaks. Symbols containing "/" (crypto) do not.
    """
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.7, 0.3],
    )
    fig.add_trace(
        go.Candlestick(
            x=bars.index,
            open=bars["open"],
            high=bars["high"],
            low=bars["low"],
            close=bars["close"],
            name=symbol,
            increasing_line_color=UP_COLOR,
            decreasing_line_color=DOWN_COLOR,
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    for label, series in overlays.items():
        fig.add_trace(
            go.Scatter(
                x=series.index, y=series.to_numpy(), mode="lines", name=label, line={"width": 1.5}
            ),
            row=1,
            col=1,
        )
    bar_colors = [
        UP_COLOR if c >= o else DOWN_COLOR for o, c in zip(bars["open"], bars["close"], strict=True)
    ]
    fig.add_trace(
        go.Bar(
            x=bars.index,
            y=bars["volume"],
            name="Volume",
            marker={"color": bar_colors, "opacity": 0.5},
            showlegend=False,
        ),
        row=2,
        col=1,
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    if "/" not in symbol:
        fig.update_xaxes(rangebreaks=[{"bounds": ["sat", "mon"]}])
    fig.update_xaxes(rangeslider_visible=False)
    fig.update_layout(title_text=symbol, height=560)
    return _style(fig)


def indicator_subplot(
    series: Mapping[str, pd.Series], title: str, hlines: Sequence[float] = ()
) -> go.Figure:
    """One oscillator panel (e.g. RSI, ATR) with optional dotted reference lines."""
    fig = go.Figure()
    for label, values in series.items():
        fig.add_trace(
            go.Scatter(
                x=values.index, y=values.to_numpy(), mode="lines", name=label, line={"width": 1.5}
            )
        )
    for level in hlines:
        fig.add_hline(y=level, line={"dash": "dot", "color": HLINE_COLOR, "width": 1})
    fig.update_layout(title_text=title, height=280)
    fig.update_yaxes(title_text=title)
    return _style(fig)


def _format_param(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def compute_overlays(
    bars: pd.DataFrame, picks: Sequence[tuple[str, dict[str, float]]]
) -> dict[str, pd.Series]:
    """Run each picked indicator from INDICATORS and return labelled series.

    ``picks`` is (indicator name, resolved params), for example ("sma", {"n": 200}).
    Labels look like "SMA 200". DataFrame outputs (macd, bbands) are split into one
    series per column, labelled e.g. "BBANDS 20 2 upper". Overlay kinds are in
    PRICE_INDICATORS (draw on candles) and OSCILLATOR_INDICATORS (own subplot).
    """
    close = bars["close"]
    overlays: dict[str, pd.Series] = {}
    for name, params in picks:
        key = name.lower()
        if key not in INDICATORS:
            raise ValueError(f"unknown indicator {name!r}")
        kwargs = {p: float(v) if p in _FLOAT_PARAMS else int(v) for p, v in params.items()}
        if key == "atr":
            result = INDICATORS[key](bars["high"], bars["low"], close, **kwargs)
        else:
            result = INDICATORS[key](close, **kwargs)
        stem = " ".join([key.upper(), *(_format_param(v) for v in params.values())])
        if isinstance(result, pd.DataFrame):
            pieces = {f"{stem} {col}": result[col] for col in result.columns}
        else:
            pieces = {stem: result}
        for label, series in pieces.items():
            if label in overlays:
                raise ValueError(f"duplicate overlay label {label!r}")
            overlays[label] = series.rename(label)
    return overlays
