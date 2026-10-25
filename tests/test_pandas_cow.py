"""Pandas CoW regression: pipeline runs warning-free on pandas 2/3 (no chained assignment)."""

import warnings

import numpy as np
import pandas as pd

from gg_bot import backtest as BT
from gg_bot import indicators as I
from gg_bot import strategy as S


def _frame(n=600, seed=7):
    px = np.linspace(100.0, 130.0, n) + np.random.RandomState(seed).randn(n)
    return pd.DataFrame(
        {
            "open": px,
            "high": px + 1.0,
            "low": px - 1.0,
            "close": px,
            "volume": np.full(n, 10.0),
        }
    )


def _quiet(fn, *args, **kwargs):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        return fn(*args, **kwargs)


def test_pipeline_warning_free_all_styles_modes():
    df = _frame()
    for style in ("Sharp", "Balanced", "Smooth"):
        for mode in ("Standard", "Advanced", "Channel"):
            p = {"style": style, "mode": mode}
            f = _quiet(S.compute_frame, df, p)
            f = _quiet(S.trend_signals, f, p)
            assert len(f) == len(df)
            assert f["trend_line"].notna().any()


def test_backtest_warning_free():
    df = _frame()
    res = _quiet(BT.run, df, {"stop_pct": 3.0, "tp_count": 4}, {})
    assert set(res) == {"trades", "winrate", "total_pct", "avg_pct"}


def test_pivot_values():
    s = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    got = _quiet(I.pivotlow, s, 2).tolist()
    assert all(np.isnan(v) for v in got[:6])
    assert got[6] == 1.0 and all(np.isnan(v) for v in got[7:])
    h = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0, 2.0, 1.0])
    goth = _quiet(I.pivothigh, h, 2).tolist()
    assert all(np.isnan(v) for v in goth[:6])
    assert goth[6] == 5.0 and all(np.isnan(v) for v in goth[7:])


def test_inputs_never_mutated():
    df = _frame()
    before = df.copy(deep=True)
    snap = _frame()
    _quiet(S.compute_frame, snap.iloc[100:], {})
    pd.testing.assert_frame_equal(snap, before)
