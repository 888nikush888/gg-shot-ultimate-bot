import pandas as pd
from gg_bot import indicators as I


def test_sma_ema_rsi_basic():
    s = pd.Series(range(1, 60), dtype=float)
    assert I.sma(s, 5).iloc[-1] == 57.0
    assert I.ema(s, 5).iloc[-1] > 55
    r = I.rsi_floor(pd.Series([10.0] * 30 + [11.0] * 30), 14)
    assert r.iloc[-1] > 50


def test_atr_positive():
    import numpy as np

    n = 50
    df = pd.DataFrame(
        {"high": np.arange(n) + 2.0, "low": np.arange(n), "close": np.arange(n) + 1.0}
    )
    a = I.atr(df.high, df.low, df.close, 14)
    assert float(a.iloc[-1]) > 0
