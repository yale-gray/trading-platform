"""HTTP client for the trading API (SPEC-004, ``/api/v1``).

This is the dashboard's only data path. It holds no trading logic and no database access.
Money comes back as decimal strings and is passed through unchanged.

Reads are cached for 60 seconds with ``st.cache_data``. Writes clear that cache.
Unreachable API -> ``ApiUnavailable``. HTTP error status -> ``ApiError``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import streamlit as st

from tradebot.config import Settings

API_PREFIX = "/api/v1"
TIMEOUT_S = 5.0
CACHE_TTL_S = 60


class ApiUnavailable(Exception):
    """The API could not be reached (not running, wrong URL, connection refused)."""


class ApiError(Exception):
    """The API answered with an error status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:200]


class ApiClient:
    """Thin wrapper over ``httpx.Client`` that maps failures to dashboard exceptions."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + API_PREFIX,
            timeout=timeout,
            transport=transport,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._http.request(method, path, params=clean or None, json=json)
        except httpx.TransportError as exc:
            raise ApiUnavailable(str(exc)) from exc
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _detail(resp))
        return resp.json()


_client: ApiClient | None = None


def get_client() -> ApiClient:
    """The shared client, built from ``Settings().api_url`` on first use."""
    global _client
    if _client is None:
        _client = ApiClient(Settings().api_url)
    return _client


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def _cached_read(path: str, params: tuple[tuple[str, Any], ...]) -> Any:
    return get_client().get(path, dict(params))


def _read(path: str, **params: Any) -> Any:
    key = tuple(sorted((k, v) for k, v in params.items() if v is not None))
    return _cached_read(path, key)


def _write(path: str, body: dict[str, Any]) -> Any:
    result = get_client().post(path, body)
    _cached_read.clear()
    return result


# ---- reads -----------------------------------------------------------------------------


def health() -> dict[str, Any]:
    return _read("/health")  # type: ignore[no-any-return]


def runner_status() -> dict[str, Any]:
    return _read("/runner/status")  # type: ignore[no-any-return]


def list_portfolios() -> list[dict[str, Any]]:
    return _read("/portfolios")  # type: ignore[no-any-return]


def get_portfolio(portfolio_id: int) -> dict[str, Any]:
    return _read(f"/portfolios/{portfolio_id}")  # type: ignore[no-any-return]


def get_equity(portfolio_id: int, sleeve: str | None = None) -> list[dict[str, Any]]:
    return _read(f"/portfolios/{portfolio_id}/equity", sleeve=sleeve)  # type: ignore[no-any-return]


def get_positions(portfolio_id: int) -> list[dict[str, Any]]:
    return _read(f"/portfolios/{portfolio_id}/positions")  # type: ignore[no-any-return]


def get_trades(portfolio_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return _read(  # type: ignore[no-any-return]
        f"/portfolios/{portfolio_id}/trades", limit=limit, offset=offset
    )


def get_orders(portfolio_id: int, status: str | None = None) -> list[dict[str, Any]]:
    return _read(f"/portfolios/{portfolio_id}/orders", status=status)  # type: ignore[no-any-return]


def get_kill_switch(portfolio_id: int) -> dict[str, Any]:
    return _read(f"/portfolios/{portfolio_id}/kill-switch")  # type: ignore[no-any-return]


def list_strategies() -> list[dict[str, Any]]:
    return _read("/strategies")  # type: ignore[no-any-return]


def strategy_cards() -> list[dict[str, Any]]:
    return _read("/strategies/cards")  # type: ignore[no-any-return]


def list_backtests(strategy_id: int | None = None) -> list[dict[str, Any]]:
    return _read("/backtests", strategy_id=strategy_id)  # type: ignore[no-any-return]


def get_backtest(run_id: int) -> dict[str, Any]:
    return _read(f"/backtests/{run_id}")  # type: ignore[no-any-return]


# ---- writes (the API never resets the kill switch; see ADR D8) -------------------------


def ack_kill_switch(portfolio_id: int, note: str) -> dict[str, Any]:
    return _write(f"/portfolios/{portfolio_id}/kill-switch/ack", {"note": note})


def create_backtest(
    strategy_id: int,
    start: date,
    end: date | None = None,
    mode: str = "single",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "strategy_id": strategy_id,
        "start": start.isoformat(),
        "mode": mode,
    }
    if end is not None:
        body["end"] = end.isoformat()
    return _write("/backtests", body)
