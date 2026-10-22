"""Shared helpers for dashboard pages: banner, API error handling, money and percent formatting."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import streamlit as st

from dashboard import client
from dashboard.client import ApiError, ApiUnavailable
from tradebot.data.universe import SP500_LABEL

START_HINT = "Start it with `uv run tradebot api serve`."
BANNER = (
    "**Simulated money only.** Nothing on this page trades real money, "
    "and nothing here is financial advice."
)

SECTIONS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Index funds", frozenset({"index", "core_momentum", "mean_reversion"})),
    ("Stocks", frozenset({"stocks"})),
    ("Crypto", frozenset({"crypto", "crypto_trend"})),
)
"""Dashboard sections by sleeve key. Keys in no section are listed under "Other"."""

OTHER = "Other"
SP500_NOTE = f"Stock universe: {SP500_LABEL}."


def section_of(sleeve_key: str) -> str:
    """The section title a sleeve belongs to (``OTHER`` when it matches none)."""
    for title, keys in SECTIONS:
        if sleeve_key in keys:
            return title
    return OTHER


def default_portfolio_index(portfolios: list[dict[str, Any]]) -> int:
    """Index of the first fund-kind portfolio, or 0 when there is none."""
    for i, p in enumerate(portfolios):
        if p.get("kind") == "fund":
            return i
    return 0


def banner() -> None:
    st.info(BANNER)


def explain(text: str) -> None:
    """Plain-language intro for a non-expert reader."""
    st.markdown(text)


def fetch[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T | None:
    """Call an API client function. On failure show a friendly message and return None."""
    try:
        return fn(*args, **kwargs)
    except ApiUnavailable:
        st.error(f"The trading API is not running, so this page has no data. {START_HINT}")
    except ApiError as exc:
        st.error(f"The API reported a problem (HTTP {exc.status}): {exc.detail}")
    return None


def runner_badge() -> None:
    """Heartbeat status of the paper-trading runner."""
    status = fetch(client.runner_status)
    if status is None:
        return
    if status["alive"]:
        st.success(f"Runner is running. Last heartbeat: {fmt_ts(status['last_heartbeat'])}.")
    else:
        st.warning(
            "Runner is not running (no recent heartbeat). "
            "Start it with `uv run tradebot runner start`."
        )


def money(value: str | None) -> str:
    """Format a decimal string as dollars. Never converts through float."""
    if value is None:
        return "—"
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        return str(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def fmt_ts(value: str | None) -> str:
    """ISO timestamp to ``YYYY-MM-DD HH:MM`` (UTC, as stored)."""
    if not value:
        return "—"
    return value[:16].replace("T", " ")


def rebased(values: list[Any]) -> list[float | None]:
    """Presentational only: scale a series so its first value is 100. No finance math."""
    nums = [None if v is None else float(v) for v in values]
    first = next((v for v in nums if v), None)
    if not first:
        return nums
    return [None if v is None else v / first * 100.0 for v in nums]
