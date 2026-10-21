"""Unit tests for the public types, errors, config and provider protocol (SPEC-001 T1)."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from enum import StrEnum
from pathlib import Path

import pandas as pd
import pytest

from tradebot.data.config import DataConfig
from tradebot.data.errors import (
    CacheCorruptError,
    DataError,
    DataQualityError,
    DataRevisionError,
    ImmutabilityError,
    ProviderError,
    StaleDataError,
    UnknownSymbolError,
)
from tradebot.data.provider import DataProvider
from tradebot.data.types import BAR_COLUMNS, AssetClass, Symbol, Timeframe

pytestmark = pytest.mark.unit


# --- types.py -------------------------------------------------------------


def test_asset_class_is_strenum_with_expected_values():
    assert issubclass(AssetClass, StrEnum)
    assert AssetClass.EQUITY == "EQUITY"
    assert AssetClass.CRYPTO == "CRYPTO"
    assert AssetClass("CRYPTO") is AssetClass.CRYPTO


def test_timeframe_is_strenum_with_d1_value_1d():
    assert issubclass(Timeframe, StrEnum)
    assert Timeframe.D1 == "1d"
    assert str(Timeframe.D1) == "1d"
    assert Timeframe("1d") is Timeframe.D1


def test_bar_columns_are_the_five_ohlcv_columns_in_order():
    assert BAR_COLUMNS == ("open", "high", "low", "close", "volume")
    assert isinstance(BAR_COLUMNS, tuple)


def test_symbol_is_a_str_newtype():
    assert Symbol("BTC/USD") == "BTC/USD"


# --- errors.py ------------------------------------------------------------


@pytest.mark.parametrize(
    "error_cls",
    [
        DataQualityError,
        DataRevisionError,
        ImmutabilityError,
        CacheCorruptError,
        StaleDataError,
        ProviderError,
        UnknownSymbolError,
    ],
)
def test_every_error_subclasses_data_error(error_cls: type[Exception]):
    assert issubclass(error_cls, DataError)
    assert issubclass(DataError, Exception)


def test_data_error_can_be_caught_as_base_class():
    with pytest.raises(DataError):
        raise UnknownSymbolError("DOGE/USD")


# --- config.py ------------------------------------------------------------


def test_data_config_honours_cache_dir_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "custom_cache"
    monkeypatch.setenv("TRADEBOT_CACHE_DIR", str(target))
    assert DataConfig().cache_dir == target


def test_data_config_expands_user_in_cache_dir_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TRADEBOT_CACHE_DIR", "~/elsewhere")
    assert DataConfig().cache_dir == tmp_path / "elsewhere"


def test_data_config_empty_cache_dir_env_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TRADEBOT_CACHE_DIR", "")
    # Default is ~/.tradebot/cache with HOME pointed at tmp_path.
    assert DataConfig().cache_dir == tmp_path / ".tradebot" / "cache"


def test_data_config_default_cache_dir_when_env_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TRADEBOT_CACHE_DIR", raising=False)
    assert DataConfig().cache_dir == tmp_path / ".tradebot" / "cache"


def test_data_config_explicit_cache_dir_overrides_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("TRADEBOT_CACHE_DIR", str(tmp_path / "from_env"))
    explicit = tmp_path / "explicit"
    assert DataConfig(cache_dir=explicit).cache_dir == explicit


def test_data_config_default_values():
    cfg = DataConfig(cache_dir=Path("/unused"))
    assert cfg.crypto_exchange == "coinbaseexchange"
    assert cfg.overlap_bars == 10
    assert cfg.equity_close_buffer_min == 60
    assert cfg.offline is False


def test_data_config_is_frozen(tmp_path: Path):
    cfg = DataConfig(cache_dir=tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.offline = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.cache_dir = tmp_path / "other"  # type: ignore[misc]


# --- provider.py ----------------------------------------------------------


class _MinimalProvider:
    name = "fake"

    def supports(self, symbol: Symbol) -> bool:
        return symbol == "SPY"

    def fetch_bars(
        self,
        symbol: Symbol,
        start,
        end,
        timeframe: Timeframe = Timeframe.D1,
    ) -> pd.DataFrame:
        return pd.DataFrame()


class _NoFetch:
    name = "broken"

    def supports(self, symbol: Symbol) -> bool:
        return True


def test_minimal_provider_satisfies_data_provider_protocol():
    assert isinstance(_MinimalProvider(), DataProvider)


def test_object_without_fetch_bars_does_not_satisfy_protocol():
    assert not isinstance(_NoFetch(), DataProvider)


def test_types_config_provider_modules_import_without_adapter_side_effects():
    # Importing the four T1 modules must not pull in the optional network adapters.
    code = (
        "import sys\n"
        "import tradebot.data.types, tradebot.data.errors, "
        "tradebot.data.config, tradebot.data.provider\n"
        "print([m for m in ('yfinance', 'ccxt') if m in sys.modules])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
