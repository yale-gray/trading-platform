"""Dashboard tests (SPEC-004 T10/T11, AC 10): AppTest smokes on canned API data, the
unreachable-API path, the import scan, and the API client. Offline and fast."""

from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard import client  # noqa: E402
from dashboard.views import common  # noqa: E402

pytestmark = pytest.mark.unit

DASHBOARD_DIR = REPO_ROOT / "dashboard"
TIMEOUT = 15
FORBIDDEN_PREFIXES = (
    "tradebot.db",
    "tradebot.portfolio",
    "tradebot.sim",
    "tradebot.risk",
    "tradebot.strategy",
    "tradebot.backtest",
    "tradebot.api",
    "tradebot.runner",
    "sqlmodel",
    "sqlalchemy",
)
API_HINT = "uv run tradebot api serve"

# ---- canned API responses (money as decimal strings, as the real API returns) ----------

BENCH = {
    "id": 2,
    "name": "bench_spy",
    "kind": "benchmark",
    "status": "active",
    "equity": "101000.00",
    "initial_cash": "100000.00",
    "total_return_pct": 1.0,
    "max_drawdown_pct": 0.5,
    "kill_switch_tripped": False,
    "last_processed_bar": "2026-10-20T00:00:00",
    "started_at": "2026-01-01T00:00:00",
}
PORTFOLIO = {
    **BENCH,
    "id": 1,
    "name": "fund",
    "kind": "fund",
    "equity": "100500.00",
    "total_return_pct": 0.5,
    "max_drawdown_pct": 1.2,
}
SLEEVE = {
    "key": "momentum",
    "strategy_name": "Trend",
    "target_weight_pct": 60.0,
    "actual_weight_pct": 58.0,
    "equity": "58000.00",
    "return_pct": 1.5,
    "n_positions": 2,
}
KILL_OFF = {
    "tripped": False,
    "tripped_at_bar": None,
    "drawdown_pct": None,
    "threshold_pct": 20.0,
    "acknowledged_at": None,
    "note": None,
}
KILL_ON = {
    "tripped": True,
    "tripped_at_bar": "2026-09-01T00:00:00",
    "drawdown_pct": 21.0,
    "threshold_pct": 20.0,
    "acknowledged_at": None,
    "note": None,
}
DETAIL = {**PORTFOLIO, "sleeves": [SLEEVE], "kill_switch": KILL_ON, "benchmarks": [BENCH]}
EQUITY = [
    {"ts": f"2026-10-{d:02d}T00:00:00", "equity": eq, "drawdown_pct": dd, "benchmark_equity": be}
    for d, eq, dd, be in [
        (1, "100000.00", 0.0, "100000.00"),
        (2, "99000.00", 1.0, "100500.00"),
        (3, "100500.00", 0.2, "101000.00"),
    ]
]
POSITION = {
    "sleeve_key": "momentum",
    "symbol": "SPY",
    "qty": "10",
    "avg_cost": "500.00",
    "mark_price": "510.00",
    "market_value": "5100.00",
    "unrealized_pnl": "100.00",
    "weight_pct": 5.0,
}
TRADE = {
    "ts": "2026-10-02T00:00:00",
    "sleeve_key": "momentum",
    "symbol": "SPY",
    "side": "BUY",
    "qty": "10",
    "price": "500.00",
    "fee": "1.00",
    "slippage": "0.50",
    "reason": "signal",
}
STRATEGY = {
    "id": 1,
    "name": "Trend",
    "version": 1,
    "market": "equity",
    "schema_": {},
    "created_at": "2026-01-01T00:00:00",
}
CARD = {
    "strategy_id": 1,
    "name": "Trend",
    "version": 1,
    "market": "equity",
    "return_1y_pct": 8.0,
    "max_drawdown_pct": 4.0,
    "sortino": 1.2,
    "benchmark_symbol": "SPY",
    "benchmark_return_1y_pct": 10.0,
    "source": "backtest",
    "as_of": "2026-10-01T00:00:00",
}
RUN = {
    "id": 7,
    "strategy_id": 1,
    "status": "done",
    "started_at": None,
    "finished_at": "2026-10-01T00:00:00",
    "total_return_pct": 8.0,
    "sharpe": 0.9,
    "sortino": 1.2,
    "max_drawdown_pct": 4.0,
    "n_trades": 1,
    "benchmark_symbol": "SPY",
}
RUN_DETAIL = {
    **RUN,
    "params": {},
    "error": None,
    "result": {"deltas": {"SPY": {"total_return": -0.02}}},
    "equity": EQUITY,
    "trades": [{**TRADE, "reason": "closed_trade"}],
}
RUNNER = {
    "alive": True,
    "last_heartbeat": "2026-10-21T10:00:00",
    "sleeves": [
        {
            "key": "fund",
            "last_processed_bar": "2026-10-20T00:00:00",
            "last_status": "ok",
            "last_error": None,
        }
    ],
}
TRADES_PAGE = {"items": [TRADE], "total": 1, "limit": 50, "offset": 0}

PAGES = {
    "portfolios": "dashboard.views.portfolios",
    "portfolio_detail": "dashboard.views.portfolio_detail",
    "strategies": "dashboard.views.strategies",
    "backtests": "dashboard.views.backtests",
    "runner": "dashboard.views.runner",
}


def _canned(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = {
        "list_portfolios": lambda: [PORTFOLIO, BENCH],
        "get_portfolio": lambda pid: DETAIL,
        "get_equity": lambda pid, sleeve=None: EQUITY,
        "get_positions": lambda pid: [POSITION],
        "get_trades": lambda pid, limit=50, offset=0: TRADES_PAGE,
        "get_kill_switch": lambda pid: KILL_ON,
        "list_strategies": lambda: [STRATEGY],
        "strategy_cards": lambda: [CARD],
        "list_backtests": lambda strategy_id=None: [RUN],
        "get_backtest": lambda run_id: RUN_DETAIL,
        "create_backtest": lambda *a, **k: {**RUN, "id": 8, "status": "queued"},
        "runner_status": lambda: RUNNER,
    }
    for name, fn in fakes.items():
        monkeypatch.setattr(client, name, fn)


def _page(module: str) -> AppTest:
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        f"from {module} import render\n"
        "render()\n"
    )
    return AppTest.from_string(script, default_timeout=TIMEOUT).run(timeout=TIMEOUT)


def _error_text(at: AppTest) -> list[str]:
    return [e.value for e in at.error]


# ---- AppTest smokes ---------------------------------------------------------------------


@pytest.mark.parametrize("name", list(PAGES))
def test_each_page_renders_on_canned_data(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _canned(monkeypatch)
    at = _page(PAGES[name])
    assert not at.exception, [e.value for e in at.exception]
    # A tripped kill switch is shown as an error box by design; API failures must not be.
    api_errors = [e for e in _error_text(at) if "API" in e or "not running" in e]
    assert not api_errors, api_errors
    assert len(at.title) == 1
    assert any("Simulated money only" in i.value for i in at.info)


def test_portfolio_detail_shows_kill_switch_reset_hint_and_ack_calls_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _canned(monkeypatch)
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        client, "ack_kill_switch", lambda pid, note: calls.append((pid, note)) or KILL_ON
    )
    at = _page(PAGES["portfolio_detail"])
    assert not at.exception, [e.value for e in at.exception]
    assert any("reset-kill-switch" in c.value for c in at.caption)
    assert any("does not reset" in c.value for c in at.caption)
    # No rerun between typing and submitting: AppTest drops pending form input otherwise.
    at.text_area(key="ack_note_1").input("checked the trip by hand")
    ack = next(b for b in at.button if b.label == "Acknowledge")
    ack.click().run(timeout=TIMEOUT)
    assert not at.exception, [e.value for e in at.exception]
    assert calls == [(1, "checked the trip by hand")]


def test_backtests_queue_form_calls_api(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned(monkeypatch)
    seen: list[dict] = []

    def fake_create(strategy_id, start, end=None, mode="single"):
        seen.append({"strategy_id": strategy_id, "start": start, "end": end, "mode": mode})
        return {**RUN, "id": 8, "status": "queued"}

    monkeypatch.setattr(client, "create_backtest", fake_create)
    at = _page(PAGES["backtests"])
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.button if b.label == "Queue backtest").click().run(timeout=TIMEOUT)
    assert not at.exception, [e.value for e in at.exception]
    assert seen == [{"strategy_id": 1, "start": date(2024, 1, 1), "end": None, "mode": "single"}]


def test_full_app_navigation_renders_default_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _canned(monkeypatch)
    at = AppTest.from_file(str(DASHBOARD_DIR / "app.py"), default_timeout=TIMEOUT).run(
        timeout=TIMEOUT
    )
    assert not at.exception, [e.value for e in at.exception]
    assert any(t.value == "Portfolios" for t in at.title)


# ---- API unreachable --------------------------------------------------------------------


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise httpx.ConnectError("connection refused")


@pytest.mark.parametrize("name", list(PAGES))
def test_unreachable_api_shows_friendly_message(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(httpx.Client, "send", _refuse)
    at = _page(PAGES[name])
    assert not at.exception, [e.value for e in at.exception]
    errors = _error_text(at)
    assert errors and all(API_HINT in e for e in errors), errors


# ---- import scan (AC 10) -----------------------------------------------------------------


def test_dashboard_never_imports_trading_or_db_code() -> None:
    files = sorted(DASHBOARD_DIR.rglob("*.py"))
    assert files, "dashboard package not found"
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")
    assert not offenders, offenders


# ---- API client and formatting ----------------------------------------------------------


def _api(handler: object) -> client.ApiClient:
    return client.ApiClient("http://test", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_client_passes_money_strings_through_unchanged() -> None:
    api = _api(lambda req: httpx.Response(200, json={"equity": "100000.10"}))
    body = api.get("/portfolios/1")
    assert body["equity"] == "100000.10"
    assert isinstance(body["equity"], str)


def test_client_builds_api_v1_paths_and_drops_none_params() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=[])

    _api(handler).get("/portfolios/1/equity", {"sleeve": None})
    assert seen[0].url.path == "/api/v1/portfolios/1/equity"
    assert "sleeve" not in str(seen[0].url)


def test_client_maps_error_status_to_api_error() -> None:
    api = _api(lambda req: httpx.Response(409, json={"detail": "not tripped"}))
    with pytest.raises(client.ApiError) as info:
        api.post("/portfolios/1/kill-switch/ack", {"note": "x"})
    assert info.value.status == 409
    assert info.value.detail == "not tripped"


def test_client_maps_connection_failure_to_unavailable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(client.ApiUnavailable):
        _api(handler).get("/health")


def test_ack_posts_to_ack_endpoint_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=KILL_ON)

    monkeypatch.setattr(client, "_client", _api(handler))
    client.ack_kill_switch(3, "reviewed")
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/api/v1/portfolios/3/kill-switch/ack")
    ]
    assert b'"note":"reviewed"' in seen[0].content.replace(b" ", b"")


def test_money_and_percent_formatting() -> None:
    assert common.money("-1234.5") == "-$1,234.50"
    assert common.money("100000.00") == "$100,000.00"
    assert common.money(None) == "—"
    assert common.pct(None) == "—"
    assert common.pct(1.234) == "1.23%"


def test_portfolio_detail_groups_by_section_and_shows_day_trading_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _canned(monkeypatch)
    sleeves = [{**SLEEVE, "key": k, "strategy_name": k} for k in ("index", "stocks", "crypto")]
    detail = {**DETAIL, "sleeves": sleeves}
    positions = [{**POSITION, "sleeve_key": "index"}, {**POSITION, "sleeve_key": "crypto"}]
    monkeypatch.setattr(client, "get_portfolio", lambda pid: detail)
    trades = {**TRADES_PAGE, "items": [{**TRADE, "sleeve_key": "stocks"}]}
    monkeypatch.setattr(client, "get_positions", lambda pid: positions)
    monkeypatch.setattr(client, "get_trades", lambda pid, limit=50, offset=0: trades)
    at = _page(PAGES["portfolio_detail"])
    assert not at.exception, [e.value for e in at.exception]
    headers = [h.value for h in at.header]
    assert headers == ["Index funds", "Stocks", "Crypto", "Day trading"]
    assert any("Alpaca data key" in i.value for i in at.info)
    from tradebot.data.universe import SP500_LABEL

    assert [c.value for c in at.caption if SP500_LABEL in c.value]  # Stocks section only
    assert len([c for c in at.caption if SP500_LABEL in c.value]) == 1


# ---- sections: Index funds, Stocks, Crypto, Day trading ---------------------------------

SECTION_PAGES = {
    "index": "dashboard.views.section_index",
    "stocks": "dashboard.views.section_stocks",
    "crypto": "dashboard.views.section_crypto",
    "daytrading": "dashboard.views.section_daytrading",
}
SECTION_TITLES = {
    "index": "Index funds",
    "stocks": "Stocks",
    "crypto": "Crypto",
    "daytrading": "Day trading",
}
NO_TRADES_TEXT = "No trades yet — the first orders fill at the next market open."
SECTION_SLEEVES = [
    {**SLEEVE, "key": k, "strategy_name": k} for k in ("index", "core_momentum", "stocks", "crypto")
]
SECTION_POSITIONS = [
    {**POSITION, "sleeve_key": "index"},
    {**POSITION, "sleeve_key": "crypto", "symbol": "BTC/USD"},
]
SECTION_TRADES = {
    **TRADES_PAGE,
    "items": [{**TRADE, "sleeve_key": "stocks"}, {**TRADE, "sleeve_key": "index"}],
}


def _section_canned(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sleeves: list[dict] = SECTION_SLEEVES,
    positions: list[dict] = SECTION_POSITIONS,
    trades: dict = SECTION_TRADES,
    equity: list[dict] = EQUITY,
    portfolios: list[dict] | None = None,
) -> None:
    _canned(monkeypatch)
    monkeypatch.setattr(
        client, "list_portfolios", lambda: [PORTFOLIO, BENCH] if portfolios is None else portfolios
    )
    monkeypatch.setattr(client, "get_portfolio", lambda pid: {**DETAIL, "sleeves": sleeves})
    monkeypatch.setattr(client, "get_positions", lambda pid: positions)
    monkeypatch.setattr(client, "get_trades", lambda pid, limit=50, offset=0: trades)
    monkeypatch.setattr(client, "get_equity", lambda pid, sleeve=None: equity)


@pytest.mark.parametrize("name", list(SECTION_PAGES))
def test_section_pages_render_on_canned_data(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _section_canned(monkeypatch)
    at = _page(SECTION_PAGES[name])
    assert not at.exception, [e.value for e in at.exception]
    assert [t.value for t in at.title] == [SECTION_TITLES[name]]
    assert any("Simulated money only" in i.value for i in at.info)
    api_errors = [e for e in _error_text(at) if "API" in e or "not running" in e]
    assert not api_errors, api_errors


def test_section_page_shows_only_its_own_sleeves(monkeypatch: pytest.MonkeyPatch) -> None:
    _section_canned(monkeypatch)
    at = _page(SECTION_PAGES["index"])
    assert not at.exception, [e.value for e in at.exception]
    positions, trades = [d.value for d in at.dataframe]
    assert set(positions["Sleeve"]) == {"index"}  # the crypto position is not shown
    assert set(trades["Sleeve"]) == {"index"}  # the stocks trade is filtered out
    assert len(trades) == 1


def test_stocks_page_carries_survivorship_caveat(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradebot.data.universe import SP500_LABEL

    _section_canned(monkeypatch)
    at = _page(SECTION_PAGES["stocks"])
    assert not at.exception, [e.value for e in at.exception]
    assert any(SP500_LABEL in c.value for c in at.caption)


def test_index_and_crypto_pages_have_no_survivorship_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradebot.data.universe import SP500_LABEL

    _section_canned(monkeypatch)
    for name in ("index", "crypto"):
        at = _page(SECTION_PAGES[name])
        assert not any(SP500_LABEL in c.value for c in at.caption), name


def test_section_without_sleeves_shows_friendly_message(monkeypatch: pytest.MonkeyPatch) -> None:
    only_crypto = [s for s in SECTION_SLEEVES if s["key"] == "crypto"]
    _section_canned(monkeypatch, sleeves=only_crypto)
    at = _page(SECTION_PAGES["index"])
    assert not at.exception, [e.value for e in at.exception]
    assert any("has no index funds sleeve" in i.value for i in at.info)
    assert not at.dataframe


def test_section_without_trades_says_first_orders_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = {**TRADES_PAGE, "items": [], "total": 0}
    _section_canned(monkeypatch, trades=empty, positions=[], equity=[])
    at = _page(SECTION_PAGES["crypto"])
    assert not at.exception, [e.value for e in at.exception]
    assert any(i.value == NO_TRADES_TEXT for i in at.info)
    assert any("no value history yet" in c.value for c in at.caption)


def test_section_with_no_portfolios_points_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    _section_canned(monkeypatch, portfolios=[])
    at = _page(SECTION_PAGES["stocks"])
    assert not at.exception, [e.value for e in at.exception]
    assert any("No portfolios yet" in i.value for i in at.info)


def test_default_portfolio_is_first_fund_kind() -> None:
    assert common.default_portfolio_index([BENCH, PORTFOLIO]) == 1
    assert common.default_portfolio_index([BENCH]) == 0
    assert common.default_portfolio_index([]) == 0


def test_day_trading_page_is_a_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("day trading page must not call the API")

    monkeypatch.setattr(client, "list_portfolios", boom)
    at = _page(SECTION_PAGES["daytrading"])
    assert not at.exception, [e.value for e in at.exception]
    assert any("Alpaca market-data key" in i.value for i in at.info)
    assert not at.dataframe


@pytest.mark.parametrize("name", ["index", "stocks", "crypto"])
def test_unreachable_api_on_section_pages_shows_friendly_message(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(httpx.Client, "send", _refuse)
    at = _page(SECTION_PAGES[name])
    assert not at.exception, [e.value for e in at.exception]
    errors = _error_text(at)
    assert errors and all(API_HINT in e for e in errors), errors
