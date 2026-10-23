"""DSR, Holm, Newey-West, overlay and T-bill edge (ADR-0001 A24, A26) against hand values."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.protocol import (
    HoldoutError,
    HoldoutLog,
    deflated_sharpe,
    edge_vs_tbills,
    expected_max_sharpe,
    hac_ols,
    holm_adjust,
    newey_west_lrv,
    spy_overlay,
)
from tradebot.data.rates import DTB3_PATH, load_dtb3

X5 = np.array([1.0, 2.0, 3.0, 4.0, 5.0])


def test_newey_west_hand_values() -> None:
    # demeaned [-2,-1,0,1,2]: g0=2, g1=0.8, g2=-0.2, g3=-0.8, g4=-0.8, g5=0 (all / n)
    u = X5 - X5.mean()
    lrv1 = newey_west_lrv(u, lags=1)  # 2 + 2 * (1 - 1/2) * 0.8
    assert lrv1 == pytest.approx(2.8)
    assert 3.0 / math.sqrt(lrv1 / 5) == pytest.approx(4.00892, abs=1e-4)
    # lag 5: 2 + 2 * (5/6*0.8 - 4/6*0.2 - 3/6*0.8 - 2/6*0.8) = 1.73333
    assert newey_west_lrv(u, lags=5) == pytest.approx(1.7333333)
    # the generic HAC regression with an intercept only reproduces the mean test
    beta, _, t = hac_ols(X5, np.ones((5, 1)), lags=1)
    assert beta[0] == pytest.approx(3.0) and t[0] == pytest.approx(4.00892, abs=1e-4)


def test_expected_max_sharpe_and_dsr_hand_example() -> None:
    # N=100, variance 0.04: sd 0.2 * ((1-g)*Z(0.99) + g*Z(1-1/(100e))) = 0.2 * 2.530603
    assert expected_max_sharpe(100, 0.04) == pytest.approx(0.506121, abs=1e-5)
    assert expected_max_sharpe(5, 0.01) == pytest.approx(0.119259, abs=1e-5)
    assert expected_max_sharpe(1, 0.04) == 0.0

    # Normal returns (skew 0, kurtosis 3): DSR = Phi((SR - SR0) * sqrt(T-1) / sqrt(1 + SR^2/2)).
    # Build a series with exactly SR = 0.1, skew 0 and kurtosis 3 is not possible by hand, so
    # check the formula pieces through the result fields instead.
    rng = np.random.default_rng(1)
    r = rng.normal(0.001, 0.01, 1000)
    res = deflated_sharpe(r, trial_sharpes=[0.0, 0.02, -0.01, 0.03, 0.05])
    sr = r.mean() / r.std(ddof=1)
    z = (r - r.mean()) / r.std()
    sk, ku = (z**3).mean(), (z**4).mean()
    var = np.var([0.0, 0.02, -0.01, 0.03, 0.05], ddof=1)
    sr0 = expected_max_sharpe(5, var)
    expect = 0.5 * math.erfc(
        -((sr - sr0) * math.sqrt(999) / math.sqrt(1 - sk * sr + (ku - 1) / 4 * sr * sr))
        / math.sqrt(2)
    )
    assert res.dsr == pytest.approx(expect, abs=1e-12)
    assert res.n_trials == 5 and res.sr0 == pytest.approx(sr0)
    # more trials -> higher bar -> lower DSR
    more = deflated_sharpe(r, trial_sharpes=[0.0, 0.02, -0.01, 0.03, 0.05], n_trials=500)
    assert more.dsr < res.dsr
    # no deflation info: plain probabilistic Sharpe ratio
    assert deflated_sharpe(r, trial_sharpes=[]).sr0 == 0.0


def test_holm_step_down() -> None:
    # p sorted: 0.01, 0.03, 0.04 with m=3 -> 0.03, max(0.03, 0.06)=0.06, max(0.06, 0.04)=0.06
    assert holm_adjust([0.04, 0.01, 0.03]) == pytest.approx([0.06, 0.03, 0.06])
    assert holm_adjust([0.5]) == [0.5]
    assert holm_adjust([0.4, 0.9]) == pytest.approx([0.8, 0.9])  # 2*0.4, then max(.8, .9)
    assert holm_adjust([0.9, float("nan")]) == [1.0, 1.0]
    assert holm_adjust([0.2, 0.001])[1] == pytest.approx(0.002)


def test_edge_vs_tbills_subtracts_the_bill_and_is_one_sided() -> None:
    idx = pd.bdate_range("2020-01-01", periods=500, tz="UTC")
    rng = np.random.default_rng(2)
    bill = pd.Series(0.0252, index=idx)  # 2.52% -> 0.0001 per day
    good = pd.Series(rng.normal(0.0101, 0.005, 500), index=idx)
    res = edge_vs_tbills(good, tbill=bill)
    assert res.mean_daily == pytest.approx(good.mean() - 0.0001)
    assert res.t_stat > 3 and res.p_value < 0.01
    cash = edge_vs_tbills(pd.Series(0.0001 + rng.normal(0, 0.005, 500), index=idx), tbill=bill)
    assert abs(cash.mean_daily) < 5e-4 and cash.p_value > 0.05
    neg = edge_vs_tbills(pd.Series(rng.normal(-0.01, 0.005, 500), index=idx), tbill=bill)
    assert neg.p_value > 0.99  # one-sided: losing money is not an edge


def test_spy_overlay_recovers_alpha_beta_and_mix_sharpe() -> None:
    idx = pd.bdate_range("2018-01-01", periods=2000, tz="UTC")
    rng = np.random.default_rng(3)
    spy = pd.Series(rng.normal(0.0004, 0.01, 2000), index=idx)
    strat = 0.0003 + 0.4 * spy + pd.Series(rng.normal(0, 0.002, 2000), index=idx)
    ov = spy_overlay(strat, spy)
    assert ov.beta == pytest.approx(0.4, abs=0.03)
    assert ov.alpha_annual == pytest.approx(0.0003 * 252, abs=0.03)
    assert ov.alpha_t > 2 and ov.adds_value
    mix = 0.5 * spy + 0.5 * strat
    assert ov.sharpe_mix == pytest.approx(mix.mean() / mix.std(ddof=1) * math.sqrt(252))
    assert ov.sharpe_spy == pytest.approx(spy.mean() / spy.std(ddof=1) * math.sqrt(252))
    # pure noise strategy: no alpha
    noise = pd.Series(rng.normal(0, 0.002, 2000), index=idx)
    assert not spy_overlay(noise, spy).alpha_t > 2


def test_dtb3_loader_forward_fills_and_is_packaged_beside_the_module() -> None:
    s = load_dtb3()
    assert DTB3_PATH.parent == DTB3_PATH.with_name("__init__.py").parent
    assert s.index[0] <= pd.Timestamp("2015-01-02", tz="UTC") and s.index[-1].year >= 2026
    assert s.notna().all() and -0.01 < s.min() and s.max() < 0.2  # decimal annual rate


def test_holdout_log_rules(tmp_path) -> None:  # type: ignore[no-untyped-def]
    log = HoldoutLog(tmp_path / "h.json")
    log.record({"id": "a", "batch": "b1"})
    with pytest.raises(HoldoutError, match="already opened"):
        log.record({"id": "a", "batch": "b2"})
    log.record({"id": "b", "batch": "b1"})
    with pytest.raises(HoldoutError, match="batch"):
        log.record({"id": "c", "batch": "b1"})
    log.record({"id": "c", "batch": "b2"})
    assert [e["id"] for e in HoldoutLog(tmp_path / "h.json").entries] == ["a", "b", "c"]


def test_edge_vs_tbills_exposure_correction_a31() -> None:
    idx = pd.bdate_range("2020-01-01", periods=300, tz="UTC")
    bill = pd.Series(0.0252, index=idx)  # 0.0001 per day
    cash = pd.Series(0.0, index=idx)
    zero = pd.Series(0.0, index=idx)
    res = edge_vs_tbills(cash, tbill=bill, exposure=zero)
    assert res.mean_daily == pytest.approx(0.0, abs=1e-15)
    rng = np.random.default_rng(5)
    r = pd.Series(rng.normal(0.001, 0.005, 300), index=idx)
    full = edge_vs_tbills(r, tbill=bill, exposure=pd.Series(1.0, index=idx))
    old = edge_vs_tbills(r, tbill=bill)
    assert full.mean_daily == pytest.approx(old.mean_daily)
    assert full.mean_daily == pytest.approx(r.mean() - 0.0001)
