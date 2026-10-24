"""Pine-compatible indicators (pure numpy/pandas, no external TA lib)."""
from __future__ import annotations
import numpy as np
import pandas as pd

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()

def stdev(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).std(ddof=0)

def highest(s: pd.Series, n: int) -> pd.Series:
    # Pine ta.highest(high[1], len): highest of previous n bars excluding current
    return s.shift(1).rolling(n, min_periods=n).max()

def lowest(s: pd.Series, n: int) -> pd.Series:
    return s.shift(1).rolling(n, min_periods=n).min()

def atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int) -> pd.Series:
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    # Pine ta.atr uses RMA (Wilder)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()

def rsi_floor(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0)
    dn = (-d).clip(lower=0)
    ru = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = ru / rd.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.fillna(100.0 * (rd == 0))
    # Pine port floors to 2 decimals: math.floor(rsi*100)/100
    return np.floor(out.fillna(50.0) * 100) / 100

def stoch_k(close: pd.Series, high: pd.Series, low: pd.Series, n: int) -> pd.Series:
    ll = low.rolling(n, min_periods=n).min()
    hh = high.rolling(n, min_periods=n).max()
    rng = (hh - ll).replace(0, np.nan)
    return ((close - ll) / rng * 100).fillna(50.0)

def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a.shift(1) <= b.shift(1)) & (a > b)

def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a.shift(1) >= b.shift(1)) & (a < b)

def pivotlow(l: pd.Series, k: int) -> pd.Series:
    out = pd.Series(np.nan, index=l.index)
    for i in range(k * 2 + 1, len(l)):
        w = l.iloc[i - k * 2 - 1:i + 1]
        # pivot confirmed k bars after the low: value at i-k
        center = len(w) - 1 - k
        if w.iloc[center] == w.min() and (w == w.iloc[center]).sum() == 1:
            out.iloc[i] = w.iloc[center]
    return out

def pivothigh(h: pd.Series, k: int) -> pd.Series:
    out = pd.Series(np.nan, index=h.index)
    for i in range(k * 2 + 1, len(h)):
        w = h.iloc[i - k * 2 - 1:i + 1]
        center = len(w) - 1 - k
        if w.iloc[center] == w.max() and (w == w.iloc[center]).sum() == 1:
            out.iloc[i] = w.iloc[center]
    return out
