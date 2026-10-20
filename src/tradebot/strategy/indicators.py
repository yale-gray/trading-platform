"""Causal technical indicators (SPEC-002 section 4).

Every output has the same index and length as its input and is float64.
Results are NaN before the first valid bar. Position t only reads bars <= t.
"""

from collections.abc import Callable

import numpy as np
import pandas as pd


def _check_n(n: int, name: str = "n") -> None:
    if n < 1:
        raise ValueError(f"{name} must be >= 1, got {n}")


def _as_float(x: pd.Series) -> pd.Series:
    return x.astype("float64")


def _unnamed(s: pd.Series) -> pd.Series:
    return pd.Series(s.to_numpy(dtype="float64"), index=s.index, name=None)


def _series(values: np.ndarray, like: pd.Series) -> pd.Series:
    return pd.Series(values, index=like.index, dtype="float64", name=None)


def _wilder(v: np.ndarray, n: int) -> np.ndarray:
    """Wilder smoothing of v, where v[0] is ignored and v[1:] must be non-NaN.

    Seed at position n is mean(v[1..n]); later t use ((n-1)*prev + v_t)/n.
    """
    out = np.full(len(v), np.nan)
    if len(v) <= n:
        return out
    out[n] = v[1 : n + 1].mean()
    for t in range(n + 1, len(v)):
        out[t] = ((n - 1) * out[t - 1] + v[t]) / n
    return out


def sma(x: pd.Series, n: int) -> pd.Series:
    """Simple moving average over the last n bars. First valid bar: n-1."""
    _check_n(n)
    x = _as_float(x)
    return _unnamed(x.rolling(n, min_periods=n).mean())


def ema(x: pd.Series, n: int) -> pd.Series:
    """Exponential moving average, alpha = 2/(n+1), seeded with the SMA of the
    first n valid values. Leading NaNs are allowed; a NaN after the first valid
    value raises ValueError. First valid bar: f + n - 1, where f is the first
    valid position.
    """
    _check_n(n)
    vals = _as_float(x).to_numpy()
    out = np.full(len(vals), np.nan)
    valid = np.flatnonzero(~np.isnan(vals))
    if valid.size == 0:
        return _series(out, x)
    f = int(valid[0])
    if np.isnan(vals[f:]).any():
        raise ValueError("ema input contains NaN after its first valid value")
    seed = f + n - 1
    if seed >= len(vals):
        return _series(out, x)
    alpha = 2.0 / (n + 1)
    out[seed] = vals[f : f + n].mean()
    for t in range(seed + 1, len(vals)):
        out[t] = alpha * vals[t] + (1 - alpha) * out[t - 1]
    return _series(out, x)


def ret(x: pd.Series, n: int, skip: int = 0) -> pd.Series:
    """Return over n bars ending skip bars ago: x[t-skip] / x[t-skip-n] - 1.
    First valid bar: n + skip.
    """
    _check_n(n)
    if skip < 0:
        raise ValueError(f"skip must be >= 0, got {skip}")
    x = _as_float(x)
    return _unnamed(x.shift(skip) / x.shift(skip + n) - 1.0)


def rsi(x: pd.Series, n: int) -> pd.Series:
    """Wilder RSI. Raises ValueError if x contains NaN. First valid bar: n.
    If average loss is 0: 100 when average gain > 0, else 50.
    """
    _check_n(n)
    x = _as_float(x)
    vals = x.to_numpy()
    if np.isnan(vals).any():
        raise ValueError("rsi input contains NaN")
    out = np.full(len(vals), np.nan)
    if len(vals) <= n:
        return _series(out, x)
    d = np.empty(len(vals))
    d[0] = np.nan
    d[1:] = np.diff(vals)
    up = np.where(np.isnan(d), np.nan, np.maximum(d, 0.0))
    down = np.where(np.isnan(d), np.nan, np.maximum(-d, 0.0))
    au = _wilder(up, n)
    ad = _wilder(down, n)
    for t in range(n, len(vals)):
        if ad[t] == 0.0:
            out[t] = 100.0 if au[t] > 0.0 else 50.0
        else:
            rs = au[t] / ad[t]
            out[t] = 100.0 - 100.0 / (1.0 + rs)
    return _series(out, x)


def macd(x: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line and histogram. Columns: macd, signal, hist.
    First valid: macd at slow-1; signal and hist at slow+signal-2.
    """
    _check_n(fast, "fast")
    _check_n(slow, "slow")
    _check_n(signal, "signal")
    if fast >= slow:
        raise ValueError(f"macd requires fast < slow, got fast={fast}, slow={slow}")
    x = _as_float(x)
    macd_line = ema(x, fast) - ema(x, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame(
        {
            "macd": macd_line.astype("float64"),
            "signal": signal_line.astype("float64"),
            "hist": hist.astype("float64"),
        },
        index=x.index,
    )


def bbands(x: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Bollinger bands with population std (ddof=0).
    Columns: mid, upper, lower, pctb, bandwidth. First valid bar: n-1.
    pctb is NaN where upper == lower; bandwidth is NaN where mid == 0.
    """
    _check_n(n)
    x = _as_float(x)
    mid = x.rolling(n, min_periods=n).mean()
    sd = x.rolling(n, min_periods=n).std(ddof=0)
    upper = mid + k * sd
    lower = mid - k * sd
    width = upper - lower
    pctb = ((x - lower) / width).where(width != 0.0)
    bandwidth = (width / mid).where(mid != 0.0)
    return pd.DataFrame(
        {
            "mid": mid.astype("float64"),
            "upper": upper.astype("float64"),
            "lower": lower.astype("float64"),
            "pctb": pctb.astype("float64"),
            "bandwidth": bandwidth.astype("float64"),
        },
        index=x.index,
    )


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """Wilder ATR. Raises ValueError on mismatched index/length or any NaN in
    the inputs. TR_0 is NaN. First valid bar: n.
    """
    _check_n(n)
    if len(high) != len(low) or len(high) != len(close):
        raise ValueError("atr inputs must have equal length")
    if not (high.index.equals(low.index) and high.index.equals(close.index)):
        raise ValueError("atr inputs must have equal index")
    h = _as_float(high).to_numpy()
    lo = _as_float(low).to_numpy()
    c = _as_float(close).to_numpy()
    if np.isnan(h).any() or np.isnan(lo).any() or np.isnan(c).any():
        raise ValueError("atr input contains NaN")
    tr = np.full(len(h), np.nan)
    if len(h) > 1:
        prev_c = c[:-1]
        tr[1:] = np.maximum.reduce(
            [h[1:] - lo[1:], np.abs(h[1:] - prev_c), np.abs(lo[1:] - prev_c)]
        )
    return _series(_wilder(tr, n), high)


def vol(x: pd.Series, n: int) -> pd.Series:
    """Sample std (ddof=1) of the last n simple returns, not annualised.
    First valid bar: n.
    """
    _check_n(n)
    x = _as_float(x)
    r = x / x.shift(1) - 1.0
    return _unnamed(r.rolling(n, min_periods=n).std(ddof=1))


INDICATORS: dict[str, Callable] = {
    "sma": sma,
    "ema": ema,
    "ret": ret,
    "rsi": rsi,
    "macd": macd,
    "bbands": bbands,
    "atr": atr,
    "vol": vol,
}
