"""Evidence-protocol statistics (ADR-0001 A24-A26): DSR, Holm, Newey-West edge, SPY overlay.

Pure numpy/stdlib (no scipy). Returns are plain floats; this is analytics, not ledger code.
The hold-out log lives here; the code that runs the hold-out is ``validation.open_holdout``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from tradebot.data.rates import load_dtb3

__all__ = [
    "DEFAULT_HOLDOUT_LOG",
    "EULER_GAMMA",
    "NW_LAGS",
    "DsrResult",
    "EdgeResult",
    "HoldoutError",
    "HoldoutLog",
    "OverlayResult",
    "daily_returns",
    "deflated_sharpe",
    "edge_vs_tbills",
    "expected_max_sharpe",
    "hac_ols",
    "holm_adjust",
    "newey_west_lrv",
    "spy_overlay",
]

NW_LAGS = 5
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()
NAN = float("nan")
DEFAULT_HOLDOUT_LOG = (
    Path(__file__).resolve().parents[3] / "docs" / "research" / ("holdout-log.json")
)


# ---------------------------------------------------------------------------- returns


def daily_returns(equity: pd.Series) -> pd.Series:
    """Simple daily returns from an equity curve (intraday curves: last value of each UTC day)."""
    s = equity.astype("float64")
    s = s.groupby(s.index.normalize()).last()
    return s.pct_change().dropna()


def daily_start_exposure(exposure: pd.Series, index: pd.Index) -> pd.Series:
    """Fraction of equity invested at the START of each day in ``index`` (A31).

    The last exposure of the previous UTC day, on the ``daily_returns`` grid; 0 when unknown.
    """
    x = exposure.astype("float64")
    x = x.groupby(x.index.normalize()).last().shift(1)
    return x.reindex(index).fillna(0.0)


def days_per_year(periods_per_year: int) -> int:
    """365 for crypto accounts (ppy 365), else 252 (daily and intraday equity accounts)."""
    return 365 if periods_per_year == 365 else 252


# ---------------------------------------------------------------------------- Newey-West


def newey_west_lrv(u: np.ndarray, lags: int = NW_LAGS) -> float:
    """Bartlett-kernel long-run variance of a (mean-zero) series, normalised by n."""
    n = len(u)
    out = float(np.dot(u, u)) / n
    for lag in range(1, min(lags, n - 1) + 1):
        out += 2.0 * (1.0 - lag / (lags + 1.0)) * float(np.dot(u[lag:], u[:-lag])) / n
    return out


def hac_ols(
    y: np.ndarray, x: np.ndarray, lags: int = NW_LAGS
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """OLS with Newey-West covariance. Returns (beta, standard errors, t-stats).

    ``x`` includes any intercept column. No small-sample correction.
    """
    n = len(y)
    xtx_inv = np.linalg.inv(x.T @ x)
    beta = xtx_inv @ (x.T @ y)
    s = x * (y - x @ beta)[:, None]
    omega = s.T @ s / n
    for lag in range(1, min(lags, n - 1) + 1):
        g = s[lag:].T @ s[:-lag] / n
        omega += (1.0 - lag / (lags + 1.0)) * (g + g.T)
    cov = n * xtx_inv @ omega @ xtx_inv
    se = np.sqrt(np.diag(cov))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, beta / se, NAN)
    return beta, se, t


# ---------------------------------------------------------------------------- A26 (a)


class EdgeResult(BaseModel):
    """Mean daily return in excess of the T-bill, one-sided test (H1: mean > 0)."""

    model_config = ConfigDict(frozen=True)

    n: int
    mean_daily: float
    mean_annual: float
    t_stat: float
    p_value: float  # one-sided
    lags: int = NW_LAGS


def edge_vs_tbills(
    returns: pd.Series,
    *,
    tbill: pd.Series | None = None,
    exposure: pd.Series | None = None,
    periods_per_year: int = 252,
    lags: int = NW_LAGS,
) -> EdgeResult:
    """Excess of daily ``returns`` over the daily T-bill rate, Newey-West t and one-sided p.

    ``tbill`` is the annual decimal rate (default: committed FRED DTB3), forward-filled onto the
    return dates; the daily rate is annual / days-per-year.

    A31: the simulator's idle cash earns 0, so the edge is measured as if idle cash earned the
    bill: excess_t = r_t - rf_t * w_t, with ``w_t`` the fraction of equity invested at the start
    of day t (``exposure``, on the same grid as ``returns``; see ``daily_start_exposure``).
    Equivalent to r_t + rf_t(1 - w_t) - rf_t. A strategy fully in cash has edge exactly 0; a fully
    invested one gets the old formula. ``exposure=None`` means fully invested (w = 1).
    """
    dpy = days_per_year(periods_per_year)
    rates = tbill if tbill is not None else load_dtb3()
    aligned = rates.reindex(rates.index.union(returns.index)).ffill().reindex(returns.index)
    w = 1.0 if exposure is None else exposure.reindex(returns.index).fillna(0.0)
    ex = (returns - (aligned / dpy) * w).dropna()
    n = len(ex)
    if n < 3:
        return EdgeResult(n=n, mean_daily=NAN, mean_annual=NAN, t_stat=NAN, p_value=1.0)
    u = ex.to_numpy(dtype="float64")
    mean = float(u.mean())
    lrv = newey_west_lrv(u - mean, lags)
    if lrv <= 0:
        return EdgeResult(n=n, mean_daily=mean, mean_annual=mean * dpy, t_stat=NAN, p_value=1.0)
    t = mean / math.sqrt(lrv / n)
    return EdgeResult(
        n=n, mean_daily=mean, mean_annual=mean * dpy, t_stat=t, p_value=1.0 - _N.cdf(t), lags=lags
    )


# ---------------------------------------------------------------------------- A26 (b)


class OverlayResult(BaseModel):
    """Strategy vs SPY: OLS alpha/beta and the 50/50 daily-rebalanced mix."""

    model_config = ConfigDict(frozen=True)

    n: int
    alpha_annual: float
    alpha_t: float
    beta: float
    sharpe_spy: float
    sharpe_mix: float  # 50% SPY + 50% strategy, rebalanced daily

    @property
    def adds_value(self) -> bool:
        """alpha t > 2 or the mix beats SPY alone."""
        return (math.isfinite(self.alpha_t) and self.alpha_t > 2.0) or (
            math.isfinite(self.sharpe_mix)
            and math.isfinite(self.sharpe_spy)
            and self.sharpe_mix > self.sharpe_spy
        )


def _sharpe(r: np.ndarray, dpy: int) -> float:
    if len(r) < 2:
        return NAN
    sd = float(np.std(r, ddof=1))
    return float(r.mean()) / sd * math.sqrt(dpy) if sd > 0 else NAN


def spy_overlay(
    returns: pd.Series,
    spy_returns: pd.Series,
    *,
    periods_per_year: int = 252,
    lags: int = NW_LAGS,
) -> OverlayResult:
    """Regress ``returns`` on ``spy_returns`` (inner-joined by date); see :class:`OverlayResult`."""
    dpy = days_per_year(periods_per_year)
    df = pd.concat([returns.rename("s"), spy_returns.rename("b")], axis=1, join="inner").dropna()
    n = len(df)
    if n < 10:
        return OverlayResult(
            n=n, alpha_annual=NAN, alpha_t=NAN, beta=NAN, sharpe_spy=NAN, sharpe_mix=NAN
        )
    y, b = df["s"].to_numpy(dtype="float64"), df["b"].to_numpy(dtype="float64")
    x = np.column_stack([np.ones(n), b])
    beta, _, t = hac_ols(y, x, lags)
    return OverlayResult(
        n=n,
        alpha_annual=float(beta[0]) * dpy,
        alpha_t=float(t[0]),
        beta=float(beta[1]),
        sharpe_spy=_sharpe(b, dpy),
        sharpe_mix=_sharpe(0.5 * b + 0.5 * y, dpy),
    )


# ---------------------------------------------------------------------------- A24 DSR + Holm


def expected_max_sharpe(n_trials: int, var_sharpe: float) -> float:
    """Expected maximum per-period Sharpe of ``n_trials`` independent zero-skill trials.

    Bailey & Lopez de Prado (2014): ``sqrt(V) * ((1-g) Z^-1(1-1/N) + g Z^-1(1-1/(N e)))``.
    """
    if n_trials < 2 or not var_sharpe > 0:
        return 0.0
    return math.sqrt(var_sharpe) * (
        (1.0 - EULER_GAMMA) * _N.inv_cdf(1.0 - 1.0 / n_trials)
        + EULER_GAMMA * _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    )


class DsrResult(BaseModel):
    """Deflated Sharpe ratio: probability the true Sharpe exceeds the best-of-N luck level."""

    model_config = ConfigDict(frozen=True)

    dsr: float
    sharpe_period: float
    sr0: float  # expected max Sharpe under the null
    n_trials: int
    n_obs: int
    skew: float
    kurtosis: float  # non-excess (normal = 3)
    var_trial_sharpes: float


def null_sharpe_variance(sharpe_period: float, n_obs: int) -> float:
    """Sampling variance of one Sharpe estimate with no skill: ``(1 + SR^2/2) / T`` per period."""
    return (1.0 + 0.5 * sharpe_period * sharpe_period) / n_obs


def deflated_sharpe(
    returns: Sequence[float] | np.ndarray,
    *,
    trial_sharpes: Sequence[float],
    n_trials: int | None = None,
    variance: float | None = None,
) -> DsrResult:
    """DSR of a candidate's per-period ``returns`` given the per-period Sharpes of all trials.

    ``N`` defaults to ``len(trial_sharpes)`` (candidate included); the variance is the sample
    variance of ``trial_sharpes``. With fewer than 2 trial Sharpes the deflation level is 0
    (the plain probabilistic Sharpe ratio). ``variance`` overrides the sample variance (the
    batch API passes the floored same-protocol variance, ADR A30).
    """
    r = np.asarray(returns, dtype="float64")
    r = r[np.isfinite(r)]
    t = len(r)
    sd = float(np.std(r, ddof=1)) if t > 1 else 0.0
    if t < 4 or not sd > 0:
        return DsrResult(
            dsr=NAN, sharpe_period=NAN, sr0=NAN, n_trials=0, n_obs=t, skew=NAN, kurtosis=NAN,
            var_trial_sharpes=NAN,
        )  # fmt: skip
    sr = float(r.mean()) / sd
    z = (r - r.mean()) / float(np.std(r, ddof=0))
    skew, kurt = float(np.mean(z**3)), float(np.mean(z**4))
    ts = [s for s in trial_sharpes if math.isfinite(s)]
    n = n_trials if n_trials is not None else len(ts)
    var = variance if variance is not None else (float(np.var(ts, ddof=1)) if len(ts) > 1 else 0.0)
    sr0 = expected_max_sharpe(n, var)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    dsr = NAN if denom <= 0 else _N.cdf((sr - sr0) * math.sqrt(t - 1.0) / math.sqrt(denom))
    return DsrResult(
        dsr=dsr, sharpe_period=sr, sr0=sr0, n_trials=n, n_obs=t, skew=skew, kurtosis=kurt,
        var_trial_sharpes=var,
    )  # fmt: skip


def holm_adjust(pvalues: Sequence[float]) -> list[float]:
    """Holm-Bonferroni step-down adjusted p-values, in input order (NaN is treated as 1)."""
    p = [1.0 if not math.isfinite(x) else min(max(x, 0.0), 1.0) for x in pvalues]
    m = len(p)
    order = sorted(range(m), key=lambda i: p[i])
    adj = [1.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p[i]))
        adj[i] = running
    return adj


# ---------------------------------------------------------------------------- hold-out log


class HoldoutError(RuntimeError):
    """A hold-out opening was refused (repeat id, or too many finalists in the batch)."""


class HoldoutLog:
    """The ledger of hold-out openings (``docs/research/holdout-log.json``)."""

    MAX_PER_BATCH = 2

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_HOLDOUT_LOG
        self.entries: list[dict[str, object]] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        )

    def check(self, strategy_id: str, batch: str) -> None:
        """Raise :class:`HoldoutError` if ``strategy_id`` was opened or ``batch`` is full."""
        if any(e["id"] == strategy_id for e in self.entries):
            raise HoldoutError(f"hold-out already opened for {strategy_id!r}")
        if sum(1 for e in self.entries if e["batch"] == batch) >= self.MAX_PER_BATCH:
            raise HoldoutError(f"batch {batch!r} already opened {self.MAX_PER_BATCH} finalists")

    def record(self, entry: dict[str, object]) -> None:
        """Append ``entry`` (must carry ``id`` and ``batch``) after re-checking, then save."""
        self.check(str(entry["id"]), str(entry["batch"]))
        self.entries.append(entry)
        self.save()

    def save(self) -> None:
        """Write the log file."""
        self.path.write_text(json.dumps(self.entries, indent=2) + "\n", encoding="utf-8")
