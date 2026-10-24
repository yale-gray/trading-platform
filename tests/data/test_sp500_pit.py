"""Point-in-time S&P 500 membership loader, alias table and Alpaca daily bars (ADR-0001 A32)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from tradebot.clock import FixedClock
from tradebot.data import universe as U
from tradebot.data.cache import BarCache
from tradebot.data.errors import DataError
from tradebot.data.pit_prices import (
    CACHE_SOURCE,
    alpaca_candidates,
    fetch_alpaca_daily,
    member_days,
    read_coverage,
)
from tradebot.data.providers.alpaca_provider import AlpacaCredentials, AlpacaProvider
from tradebot.data.symbols import normalize_symbol
from tradebot.data.types import Symbol, Timeframe

pytestmark = pytest.mark.unit

FAKE = (
    ("AAA", date(2020, 1, 2), None),  # member throughout
    ("BBB", date(2020, 1, 2), date(2020, 1, 8)),  # last list containing it is dated 2020-01-07
    ("CCC", date(2020, 1, 6), None),  # joins Monday 2020-01-06
    ("DDD", date(2020, 1, 2), date(2020, 1, 3)),
    ("DDD", date(2020, 1, 7), None),  # re-entry
)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(U, "_sp500_intervals", lambda: FAKE)


def test_committed_history_file_has_provenance_and_parses() -> None:
    text = (U.UNIVERSE_DIR / U.SP500_HISTORY_FILE).read_text(encoding="utf-8")
    head = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert any("github.com/fja05680/sp500" in ln for ln in head)
    assert any("Retrieved 2026-10-23" in ln for ln in head)
    assert U.sp500_history_start() == date(2014, 6, 1)


def test_membership_in_force_at_previous_close(fake: None) -> None:
    # CCC enters with the list dated Monday 2020-01-06: not tradable-as-member until Tuesday
    assert "CCC" not in U.sp500_members(date(2020, 1, 6))
    assert "CCC" in U.sp500_members(date(2020, 1, 7))
    # BBB's end is exclusive: still in the list dated 2020-01-07, so a member on Wed 2020-01-08
    assert "BBB" in U.sp500_members(date(2020, 1, 8))
    assert "BBB" not in U.sp500_members(date(2020, 1, 9))
    # Monday uses Friday's close; weekend dates use the same
    assert "DDD" not in U.sp500_members(date(2020, 1, 6))
    assert "DDD" in U.sp500_members(date(2020, 1, 8))
    assert U.sp500_members(date(2020, 1, 11)) == U.sp500_members(date(2020, 1, 13)) - {"x"}


def test_before_history_start_raises(fake: None) -> None:
    with pytest.raises(DataError):
        U.sp500_members(date(2020, 1, 2))  # previous close 2019-12-31 predates the first row


def test_ever_window_semantics(fake: None) -> None:
    assert U.sp500_ever(date(2020, 1, 6), date(2020, 1, 6)) >= {"AAA", "BBB"}
    assert "CCC" not in U.sp500_ever(date(2020, 1, 6), date(2020, 1, 6))  # not yet known
    assert "CCC" in U.sp500_ever(date(2020, 1, 6), date(2020, 1, 7))
    assert "BBB" not in U.sp500_ever(date(2020, 1, 9), date(2020, 3, 1))
    assert U.sp500_ever(date(2020, 3, 1), date(2020, 1, 1)) == frozenset()


def test_flags_agree_with_members(fake: None) -> None:
    days = [d.date() for d in pd.bdate_range("2020-01-06", "2020-01-17")]
    for sym in ("AAA", "BBB", "CCC", "DDD", "ZZZ"):
        flags = U.sp500_member_flags(sym, days)
        assert flags == [1.0 if sym in U.sp500_members(d) else 0.0 for d in days]


def test_real_history_known_facts() -> None:
    m0, m1 = U.sp500_members(date(2015, 6, 1)), U.sp500_members(date(2023, 12, 29))
    assert 495 <= len(m0) <= 510 and 495 <= len(m1) <= 510
    assert {"AAPL", "BRK.B"} <= m0 and {"AAPL", "BRK.B"} <= m1
    assert "META" in m0 and "FB" not in m0  # the alias maps the source's FB onto META
    assert "SPLS" in U.sp500_members(date(2017, 9, 11))  # Staples, removed mid-September 2017
    assert "SPLS" not in U.sp500_members(date(2017, 9, 25))
    assert "TSLA" not in U.sp500_members(date(2020, 11, 20)) and "TSLA" in U.sp500_members(
        date(2020, 12, 22)
    )
    ever = U.sp500_ever(date(2015, 6, 1), date(2023, 12, 31))
    assert 650 <= len(ever) <= 780 and {"SPLS", "TWX", "META"} <= ever and "FB" not in ever
    assert m0 <= ever and m1 <= ever


def test_alias_table_is_normalised_and_complete() -> None:
    aliases = U._sp500_aliases()
    assert aliases["FB"] == "META"
    assert all(normalize_symbol(v) == v for v in aliases.values())
    assert alpaca_candidates(Symbol("META")) == ["META", "FB"]
    assert alpaca_candidates(Symbol("AAPL")) == ["AAPL"]


# ---------------------------------------------------------------- Alpaca daily bars


def _row(day: str, px: float) -> dict:
    # Alpaca stamps a daily bar at midnight New York time (05:00Z in winter, 04:00Z in summer)
    t = pd.Timestamp(day, tz="America/New_York").tz_convert("UTC")
    return {
        "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": px,
        "h": px + 1,
        "l": px - 1,
        "c": px + 0.5,
        "v": 1000,
    }


class Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload: dict) -> None:
        self._p = payload

    def json(self) -> dict:
        return self._p


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def get(self, url: str, *, params: dict, headers: dict, timeout: float) -> Resp:
        self.calls.append(dict(params))
        return Resp(self.payload)


CLOCK = FixedClock(datetime(2024, 6, 13, 12, 0, tzinfo=UTC))


def _provider(client: FakeClient) -> AlpacaProvider:
    return AlpacaProvider(
        credentials=AlpacaCredentials("PKTEST", "SECRET"),
        client=client,  # type: ignore[arg-type]
        clock=CLOCK,
        sleep=lambda s: None,
    )


def test_daily_frame_labels_new_york_session_date_and_requests_adjusted_1day() -> None:
    rows = [_row("2023-12-27", 100), _row("2023-12-28", 101), _row("2023-07-03", 90)]
    client = FakeClient({"bars": {"AAA": rows}, "next_page_token": None})
    out = _provider(client).fetch_many(
        [Symbol("AAA"), Symbol("NONE")], date(2023, 12, 1), date(2023, 12, 31), Timeframe.D1
    )
    frame = out["AAA"]
    assert [d.isoformat() for d in frame.index.date] == ["2023-12-27", "2023-12-28"]  # window
    assert str(frame.index.tz) == "UTC" and (frame.index == frame.index.normalize()).all()
    assert (frame.dtypes == "float64").all() and frame.index.name == "ts"
    assert out["NONE"].empty
    p = client.calls[0]
    assert p["timeframe"] == "1Day" and p["adjustment"] == "all"
    assert p["feed"] == "sip" and "paper" not in str(p)


def test_fetch_alpaca_daily_writes_into_the_normal_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(U, "_sp500_intervals", lambda: (("AAA", date(2024, 1, 2), None),))
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-05-01", "2024-05-31")]
    rows = [_row(d, 100 + i) for i, d in enumerate(days)]
    client = FakeClient({"bars": {"AAA": rows}, "next_page_token": None})
    cache = BarCache(tmp_path, CLOCK)
    res = fetch_alpaca_daily(
        [Symbol("AAA"), Symbol("MSNG")],
        date(2024, 5, 1),
        date(2024, 5, 31),
        cache=cache,
        provider=_provider(client),
        clock=CLOCK,
    )
    assert res.from_alpaca == ["AAA"] and list(res.failed) == ["MSNG"]
    assert cache.exists(CACHE_SOURCE, "AAA")
    got = cache.read(CACHE_SOURCE, "AAA", include_audit=True)
    assert set(got["source_rev"]) == {"alpaca_sip"}
    # an existing cache entry is never replaced
    again = fetch_alpaca_daily(
        [Symbol("AAA")], date(2024, 5, 1), date(2024, 5, 31),
        cache=cache, provider=_provider(client), clock=CLOCK,
    )  # fmt: skip
    assert again.from_alpaca == [] and cache.current_gen(CACHE_SOURCE, "AAA") == 1


def test_read_coverage_counts_member_days(tmp_path, fake: None) -> None:
    cache = BarCache(tmp_path, CLOCK)
    sessions_ = member_days(Symbol("AAA"), date(2020, 1, 7), date(2020, 1, 15))
    assert len(sessions_) == 7
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in sessions_[:5]], name="ts")
    frame = pd.DataFrame(
        {c: 100.0 for c in ("open", "high", "low", "close", "volume")}, index=idx
    ).astype(float)
    cache.create(CACHE_SOURCE, Symbol("AAA"), frame, "adj_close_as_of_2024-06-13", "alpaca_sip")
    cov = read_coverage(cache, Symbol("AAA"), date(2020, 1, 7), date(2020, 1, 15))
    assert (cov.member_days, cov.covered_days, cov.source) == (7, 5, "alpaca_sip")
    assert read_coverage(cache, Symbol("ZZZ"), date(2020, 1, 7), date(2020, 1, 15)).source == "none"


def test_dated_alias_splits_a_reused_ticker() -> None:
    """IR was Ingersoll-Rand (now Trane, TT) until 2020-03-02, then a different company."""
    early, late = U.sp500_members(date(2016, 1, 5)), U.sp500_members(date(2021, 1, 5))
    assert "TT" in early and "IR" not in early
    assert "TT" in late and "IR" in late
    assert "WYND" not in U.sp500_members(date(2017, 1, 4)) and "TNL" in U.sp500_members(
        date(2017, 1, 4)
    )
