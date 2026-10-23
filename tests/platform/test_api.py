"""API contract tests (SPEC-004 T6-T9, AC 9 and the API half of AC 15) on a runner-seeded DB."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from runner_support import DATA_START, START, SYNTH_NOW, after_close, make_data, make_runner, seed
from sqlalchemy import Engine
from sqlmodel import select

from tradebot.api.app import create_app
from tradebot.api.jobs import fail_stale_runs
from tradebot.clock import FixedClock
from tradebot.config import Settings
from tradebot.db import repo, session_scope
from tradebot.db.models import BacktestRun, BacktestTrade, KillSwitchEvent, RunnerHeartbeat
from tradebot.portfolio.config import default_config
from tradebot.risk.config import RiskConfig
from tradebot.runner import benchmark_definition, fund_definition
from tradebot.strategy.io import STRATEGIES_DIR

NOW = after_close(date(2019, 3, 1))
V1 = "/api/v1"
JSON = {"content-type": "application/json"}


@pytest.fixture
def client(db: Engine, market_cache: Path) -> Iterator[TestClient]:
    """Benchmark + hair-trigger fund (trips on 2019-01-04) processed to 2019-03-01."""
    hair = default_config().model_copy(
        update={
            "risk": RiskConfig(kill_switch_drawdown=Decimal("0.0005"), kill_switch_pause_bars=1000)
        }
    )
    seed(
        db,
        [
            benchmark_definition("SPY", START, data_start=DATA_START),
            fund_definition(START, data_start=DATA_START, engine=hair, benchmark="bench_spy"),
        ],
    )
    make_runner(db, make_data(market_cache)).tick(NOW)
    with session_scope(db) as s:
        s.add(RunnerHeartbeat(id=1, ts=repo.naive_utc(SYNTH_NOW), pid=1, version="t"))
    app = create_app(
        Settings(),
        engine=db,
        data_factory=lambda: make_data(market_cache),
        clock=FixedClock(SYNTH_NOW),
    )
    with TestClient(app) as c:
        yield c


def test_health_and_runner_status(client: TestClient) -> None:
    r = client.get(f"{V1}/health")
    assert r.status_code == 200 and r.json()["db_ok"] is True
    st = client.get(f"{V1}/runner/status").json()
    assert st["alive"] is True
    assert {s["key"] for s in st["sleeves"]} == {"bench_spy", "paper_fund"}


def test_portfolio_list_detail_money_is_a_decimal_string(client: TestClient) -> None:
    rows = client.get(f"{V1}/portfolios").json()
    assert [p["name"] for p in rows] == ["bench_spy", "paper_fund"]
    fund = rows[1]
    assert fund["initial_cash"] == "100000.00"
    assert isinstance(fund["equity"], str) and Decimal(fund["equity"]) > 0
    assert fund["kill_switch_tripped"] is True and fund["max_drawdown_pct"] > 0.05
    detail = client.get(f"{V1}/portfolios/{fund['id']}").json()
    assert {s["key"] for s in detail["sleeves"]} == {
        "core_momentum",
        "mean_reversion",
        "crypto_trend",
        "cash",
    }
    assert sum(s["target_weight_pct"] for s in detail["sleeves"]) == pytest.approx(100.0)
    assert [b["name"] for b in detail["benchmarks"]] == ["bench_spy"]  # AC 8 benchmark field
    assert detail["kill_switch"]["threshold_pct"] == pytest.approx(0.05)
    assert client.get(f"{V1}/portfolios/999").status_code == 404


def test_new_status_fields(client: TestClient) -> None:
    st = client.get(f"{V1}/runner/status").json()
    nxt = st["next_run_after"]  # SYNTH_NOW is in the past relative to nothing: just a UTC instant
    assert nxt is not None and nxt.endswith(("Z", "+00:00"))
    fund = client.get(f"{V1}/portfolios").json()[1]
    detail = client.get(f"{V1}/portfolios/{fund['id']}").json()
    assert detail["caveats"] == []
    ks = detail["kill_switch"]
    assert ks["tripped"] is True and ks["pause_ends_after_bar"] > ks["tripped_at_bar"][:10]


def test_pause_end_bar_and_next_run_calendar() -> None:
    from datetime import UTC, datetime

    from tradebot.runner.clock import next_equity_run_after, pause_end_bar

    # Fri 2026-10-23 trip, equity only: 1st bar after is Mon 10-26, 3rd is Wed 10-28
    assert pause_end_bar(["equity"], date(2026, 10, 23), 3) == date(2026, 10, 28)
    assert pause_end_bar(["crypto", "equity"], date(2026, 10, 23), 3) == date(2026, 10, 26)
    # Sat 2026-10-24 12:00 UTC -> next completion is Mon 10-26 close 20:00 UTC (EDT) + 30 min
    assert next_equity_run_after(datetime(2026, 10, 24, 12, tzinfo=UTC)) == datetime(
        2026, 10, 26, 20, 30, tzinfo=UTC
    )


def test_stocks_sleeve_gets_the_survivorship_caveat(db: Engine) -> None:
    from tradebot.api.routers.portfolios import get_portfolio
    from tradebot.data.universe import SP500_LABEL

    class _S:
        id = "stocks_momentum"

    class _D:
        sleeves = (type("Sl", (), {"strategy": _S()})(),)

    import tradebot.api.routers.portfolios as mod

    orig = mod.PortfolioDef.from_json
    mod.PortfolioDef.from_json = staticmethod(lambda _t: _D())  # type: ignore[assignment]
    try:
        with session_scope(db) as s:
            seed(db, [benchmark_definition("SPY", START, data_start=DATA_START)])
            p = repo.list_portfolios(s)[0]
            assert p.id is not None
            # summary path reads config_json only through from_json above plus KS risk json
            out = get_portfolio(s, p.id)
        assert out.caveats == [SP500_LABEL]
    finally:
        mod.PortfolioDef.from_json = orig  # type: ignore[method-assign]


def test_equity_series_has_benchmark_and_filters(client: TestClient) -> None:
    pid = client.get(f"{V1}/portfolios").json()[1]["id"]
    pts = client.get(f"{V1}/portfolios/{pid}/equity").json()
    assert len(pts) > 30 and pts[0]["ts"].startswith("2019-01-02")
    assert sum(p["benchmark_equity"] is not None for p in pts) > 20  # weekends: no SPY bar
    assert max(p["drawdown_pct"] for p in pts) > 0.05
    sub = client.get(
        f"{V1}/portfolios/{pid}/equity", params={"from": "2019-02-01", "to": "2019-02-10"}
    )
    assert [p["ts"][:10] for p in sub.json()][0] == "2019-02-01"
    assert sub.json()[-1]["ts"][:10] == "2019-02-10"
    sleeve = client.get(f"{V1}/portfolios/{pid}/equity", params={"sleeve": "core_momentum"}).json()
    assert len(sleeve) == len(pts) and sleeve[0]["equity"] != pts[0]["equity"]
    assert all(p["benchmark_missing"] is False for p in pts)  # weekends are not "missing"
    assert client.get(f"{V1}/portfolios/{pid}/equity", params={"sleeve": "nope"}).status_code == 404


def test_positions_trades_orders(client: TestClient) -> None:
    pid = client.get(f"{V1}/portfolios").json()[1]["id"]
    for pos in client.get(f"{V1}/portfolios/{pid}/positions").json():
        assert Decimal(pos["qty"]) > 0 and isinstance(pos["market_value"], str)
    page = client.get(f"{V1}/portfolios/{pid}/trades", params={"limit": 3}).json()
    assert page["limit"] == 3 and len(page["items"]) <= 3 and page["total"] >= len(page["items"])
    if page["total"] > 3:
        nxt = client.get(f"{V1}/portfolios/{pid}/trades", params={"limit": 3, "offset": 3}).json()
        assert nxt["items"] != page["items"]
    for t in page["items"]:
        assert t["side"] in {"BUY", "SELL"} and Decimal(t["price"]) > 0
    filt = client.get(f"{V1}/portfolios/{pid}/trades", params={"symbol": "NOPE"}).json()
    assert filt["total"] == 0
    orders = client.get(f"{V1}/portfolios/{pid}/orders").json()
    assert orders and {"order_id", "status", "qty_requested"} <= set(orders[0])
    pending = client.get(f"{V1}/portfolios/{pid}/orders", params={"status": "PENDING"}).json()
    assert all(o["status"] == "PENDING" for o in pending)


def test_ack_leaves_a_tripped_switch_tripped(client: TestClient, db: Engine) -> None:
    """AC 15 (API half): ack records review only; there is no reset route."""
    pid = client.get(f"{V1}/portfolios").json()[1]["id"]
    before = client.get(f"{V1}/portfolios/{pid}/kill-switch").json()
    assert before["tripped"] is True and before["acknowledged_at"] is None
    r = client.post(f"{V1}/portfolios/{pid}/kill-switch/ack", json={"note": "looked at it"})
    assert r.status_code == 200
    body = r.json()
    assert body["tripped"] is True and body["note"] == "looked at it"
    assert body["acknowledged_at"] is not None
    assert client.get(f"{V1}/portfolios/{pid}/kill-switch").json()["tripped"] is True
    with session_scope(db) as s:
        assert [e.kind for e in s.exec(select(KillSwitchEvent))] == ["trip", "ack"]
    assert client.post(f"{V1}/portfolios/{pid}/kill-switch/reset", json={}).status_code == 404
    assert (
        client.post(f"{V1}/portfolios/{pid}/kill-switch/ack", json={"note": ""}).status_code == 422
    )
    spy = client.get(f"{V1}/portfolios").json()[0]["id"]
    assert (
        client.post(f"{V1}/portfolios/{spy}/kill-switch/ack", json={"note": "x"}).status_code == 409
    )


def test_no_order_placing_or_portfolio_creating_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    writes = {
        (m.upper(), path)
        for path, ops in paths.items()
        for m in ops
        if m in {"post", "put", "delete", "patch"}
    }
    assert writes == {
        ("POST", f"{V1}/portfolios/{{portfolio_id}}/kill-switch/ack"),
        ("POST", f"{V1}/strategies"),
        ("POST", f"{V1}/backtests"),
    }


def _spec() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((STRATEGIES_DIR / "core_momentum.yaml").read_text())


def test_strategies_crud_validates_with_the_schema(client: TestClient) -> None:
    ok = client.post(
        f"{V1}/strategies", json={"name": "mine", "market": "equity", "schema_": _spec()}
    )
    assert ok.status_code == 200, ok.text
    sid = ok.json()["id"]
    again = client.post(
        f"{V1}/strategies", json={"name": "mine", "market": "equity", "schema_": _spec()}
    )
    assert again.json()["id"] == sid  # identical spec dedupes
    bad = client.post(
        f"{V1}/strategies", json={"name": "bad", "market": "equity", "schema_": {"kind": "x"}}
    )
    assert bad.status_code == 422
    wrong = client.post(
        f"{V1}/strategies", json={"name": "w", "market": "crypto", "schema_": _spec()}
    )
    assert wrong.status_code == 422
    assert client.get(f"{V1}/strategies/{sid}").json()["name"] == "mine"
    assert client.get(f"{V1}/strategies/9999").status_code == 404
    names = {s["name"] for s in client.get(f"{V1}/strategies").json()}
    assert "mine" in names
    cards = client.get(f"{V1}/strategies/cards").json()
    mine = next(c for c in cards if c["strategy_id"] == sid)
    assert mine["source"] == "backtest" and mine["return_1y_pct"] is None
    assert mine["benchmark_symbol"] == "SPY"


def test_backtest_job_runs_persists_and_feeds_the_card(client: TestClient, db: Engine) -> None:
    sid = client.post(
        f"{V1}/strategies", json={"name": "mom", "market": "equity", "schema_": _spec()}
    ).json()["id"]
    r = client.post(
        f"{V1}/backtests", json={"strategy_id": sid, "start": "2019-01-02", "end": "2019-12-31"}
    )
    assert r.status_code == 202 and r.json()["status"] == "queued"
    run_id = r.json()["id"]
    client.app.state.jobs.wait_idle(60)  # type: ignore[attr-defined]
    detail = client.get(f"{V1}/backtests/{run_id}").json()
    assert detail["status"] == "done", detail["error"]
    assert detail["benchmark_symbol"] == "SPY" and detail["n_trades"] is not None
    assert len(detail["equity"]) > 200 and detail["equity"][0]["benchmark_equity"] is not None
    assert isinstance(detail["equity"][0]["equity"], str)
    assert detail["result"]["metrics"]["total_return"] == pytest.approx(
        detail["total_return_pct"] / 100.0
    )
    assert detail["params"]["mode"] == "single"
    assert len(detail["trades"]) == detail["n_trades"] or detail["n_trades"] >= len(
        detail["trades"]
    )
    assert [x["id"] for x in client.get(f"{V1}/backtests", params={"strategy_id": sid}).json()] == [
        run_id
    ]
    card = next(c for c in client.get(f"{V1}/strategies/cards").json() if c["strategy_id"] == sid)
    assert card["return_1y_pct"] == pytest.approx(detail["total_return_pct"])
    assert card["max_drawdown_pct"] == pytest.approx(detail["max_drawdown_pct"])
    assert card["benchmark_return_1y_pct"] is not None


def test_backtest_payload_carries_caveats_and_all_trips(client: TestClient) -> None:
    from tradebot.data.universe import SP500_LABEL

    for sid_, caveats in (("core_momentum", []), ("stocks_probe", [SP500_LABEL])):
        spec = {**_spec(), "id": sid_}
        sid = client.post(
            f"{V1}/strategies", json={"name": sid_, "market": "equity", "schema_": spec}
        ).json()["id"]
        run_id = client.post(
            f"{V1}/backtests", json={"strategy_id": sid, "start": "2019-01-02", "end": "2019-06-28"}
        ).json()["id"]
        client.app.state.jobs.wait_idle(60)  # type: ignore[attr-defined]
        detail = client.get(f"{V1}/backtests/{run_id}").json()
        assert detail["status"] == "done", detail["error"]
        assert detail["result"]["caveats"] == caveats
        assert isinstance(detail["result"]["killed_dates"], list)


def test_backtest_failure_is_recorded_and_bad_requests_rejected(client: TestClient) -> None:
    sid = client.post(
        f"{V1}/strategies", json={"name": "mom2", "market": "equity", "schema_": _spec()}
    ).json()["id"]
    assert (
        client.post(f"{V1}/backtests", json={"strategy_id": 999, "start": "2019-01-02"}).status_code
        == 404
    )
    bad = client.post(
        f"{V1}/backtests", json={"strategy_id": sid, "start": "2019-01-02", "overrides": {"x": 1}}
    )
    assert bad.status_code == 422
    # a window with no data -> the job fails and says why
    r = client.post(
        f"{V1}/backtests", json={"strategy_id": sid, "start": "2010-01-02", "end": "2010-06-01"}
    )
    client.app.state.jobs.wait_idle(60)  # type: ignore[attr-defined]
    got = client.get(f"{V1}/backtests/{r.json()['id']}").json()
    assert got["status"] == "failed" and got["error"]


BAD_OVERRIDES: list[dict] = [  # type: ignore[type-arg]
    {"initial_capital": "abc"},
    {"initial_capital": 0},
    {"initial_capital": "-5"},
    {"initial_capital": "100.123"},
    {"initial_capital": "NaN"},
    {"initial_capital": True},
    {"initial_capital": [1]},
    {"rf_annual": "x"},
    {"rf_annual": -0.01},
    {"rf_annual": 0.21},
    {"rf_annual": float("inf")},
    {"rf_annual": True},
    {"kill_switch": "off"},
    {"max_asset_fraction": 0},
    {"max_asset_fraction": 1.5},
    {"max_crypto_fraction": True},
]


def test_backtest_rejects_bad_override_values(client: TestClient) -> None:
    sid = client.post(
        f"{V1}/strategies", json={"name": "mo", "market": "equity", "schema_": _spec()}
    ).json()["id"]
    for overrides in BAD_OVERRIDES:
        body = {"strategy_id": sid, "start": "2019-01-02", "overrides": overrides}
        r = client.post(f"{V1}/backtests", content=json.dumps(body), headers=JSON)
        assert r.status_code == 422, overrides
    assert client.get(f"{V1}/backtests").json() == []

    def post_name(name: str) -> int:
        body = {"name": name, "market": "equity", "schema_": _spec()}
        return client.post(f"{V1}/strategies", json=body).status_code

    assert post_name("n" * 100) == 200  # name limit: 1..100 characters
    assert post_name("n" * 101) == 422
    assert post_name("") == 422


def test_backtest_accepts_valid_overrides_and_refuses_end_after_last_closed_bar(
    client: TestClient,
) -> None:
    sid = client.post(
        f"{V1}/strategies", json={"name": "mo", "market": "equity", "schema_": _spec()}
    ).json()["id"]
    # the app clock is SYNTH_NOW (2025-01-01 12:00 UTC, a holiday): the last closed bar is
    # 2024-12-31. The cache has no such data, so an accepted run fails fast in the worker.
    ok = {
        "strategy_id": sid,
        "start": "2019-01-02",
        "end": "2024-12-31",
        "overrides": {
            "initial_capital": "50000.25",
            "rf_annual": 0.2,
            "kill_switch": False,
            "max_asset_fraction": 0.5,
        },
    }
    assert client.post(f"{V1}/backtests", json=ok).status_code == 202
    client.app.state.jobs.wait_idle(60)  # type: ignore[attr-defined]
    late = {"strategy_id": sid, "start": "2019-01-02", "end": "2025-01-01"}
    r = client.post(f"{V1}/backtests", json=late)
    assert r.status_code == 422 and "last closed bar" in r.json()["detail"]
    assert len(client.get(f"{V1}/backtests").json()) == 1


def test_equity_flags_missing_benchmark_snapshots(
    db: Engine, market_cache: Path, tmp_path: Path
) -> None:
    seed(db, [fund_definition(START, data_start=DATA_START, benchmark="bench_spy")])
    make_runner(db, make_data(market_cache)).tick(after_close(date(2019, 1, 4)))
    app = create_app(Settings(), engine=db, clock=FixedClock(SYNTH_NOW))
    with TestClient(app) as c:
        pid = c.get(f"{V1}/portfolios").json()[0]["id"]
        pts = c.get(f"{V1}/portfolios/{pid}/equity").json()
    assert pts and all(p["benchmark_equity"] is None and p["benchmark_missing"] for p in pts)


def test_backtest_trades_report_no_slippage_instead_of_a_fake_zero(
    client: TestClient, db: Engine
) -> None:
    sid = client.post(
        f"{V1}/strategies", json={"name": "mo", "market": "equity", "schema_": _spec()}
    ).json()["id"]
    with session_scope(db) as s:
        run = BacktestRun(strategy_config_id=sid, status="done", created_at=repo.naive_utc(NOW))
        s.add(run)
        s.flush()
        s.add(
            BacktestTrade(
                run_id=run.id or 0,
                symbol="SPY",
                entry_ts=datetime(2019, 1, 3),
                exit_ts=datetime(2019, 1, 10),
                qty=1.0,
                entry_px=100.0,
                exit_px=101.0,
                fees=0.5,
            )
        )
        rid = run.id
    trades = client.get(f"{V1}/backtests/{rid}").json()["trades"]
    assert len(trades) == 1 and trades[0]["slippage"] is None and trades[0]["fee"] == "0.5"


def test_stale_running_rows_are_failed_on_startup(db: Engine, market_cache: Path) -> None:
    now = repo.naive_utc(SYNTH_NOW)
    with session_scope(db) as s:
        s.add(BacktestRun(status="running", created_at=now))
        s.add(BacktestRun(status="done", created_at=now))
    assert fail_stale_runs(db, now) == 1
    app = create_app(Settings(), engine=db, clock=FixedClock(SYNTH_NOW))
    with TestClient(app) as c:
        statuses = sorted(r["status"] for r in c.get(f"{V1}/backtests").json())
    assert statuses == ["done", "failed"]
    with session_scope(db) as s:
        failed = s.exec(select(BacktestRun).where(BacktestRun.status == "failed")).one()
        assert failed.error and "interrupted" in failed.error
        assert json.loads(failed.params_json) == {}
