import inspect
from pathlib import Path
import numpy as np
import pandas as pd
from gg_bot.orders import stop_market_type
from gg_bot.engine import (
    send_market_close,
    closed_frame,
    meets_min_notional,
    should_send_error,
    cached_equity,
    save_open_if_changed,
    _EQ_CACHE,
)
from gg_bot import cli


class FakeEx:
    def __init__(self, eid):
        self.id = eid


def test_stop_market_type_per_exchange():
    assert stop_market_type(FakeEx("binanceusdm")) == "STOP_MARKET"
    assert stop_market_type(FakeEx("binance")) == "STOP_MARKET"
    assert stop_market_type(FakeEx("hyperliquid")) == "stopMarket"
    assert stop_market_type(FakeEx("aster")) == "stopMarket"


def test_native_skips_exchange_handled_closes():
    for reason in ["TP", "SL", "BE", "TRAIL"]:
        assert send_market_close("exchange_native", reason, True) is False
    for reason in ["OPP", "OPP_REVERSE", "OSC"]:
        assert send_market_close("exchange_native", reason, True) is True
    for reason in ["TP", "SL", "BE", "TRAIL", "OPP", "OSC"]:
        assert send_market_close("bot_managed", reason, True) is True
        assert send_market_close("exchange_native", reason, False) is True


def test_closed_frame_drops_forming_candle():
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    out = closed_frame(df)
    assert len(out) == 2 and list(out["close"]) == [1.0, 2.0]
    one = pd.DataFrame({"close": [1.0]})
    assert len(closed_frame(one)) == 1


def test_close_all_flag_is_real_option():
    sig = inspect.signature(cli.positions_close_all)
    default = sig.parameters["i_understand_live"].default
    assert not isinstance(default, bool), (
        "Option im Funktionskoerper statt in Parametern"
    )
    assert "--i-understand-live" in (default.param_decls or ())


def test_env_example_has_binance_keys():
    txt = Path(".env.example").read_text(encoding="utf-8")
    assert "BINANCE_API_KEY=" in txt and "BINANCE_API_SECRET=" in txt


class _LimEx:
    def __init__(self, limits=None, fail=False):
        self._limits = limits
        self._fail = fail

    def load_markets(self):
        if self._fail:
            raise RuntimeError("offline")

    def market(self, _symbol):
        return {
            "limits": {
                "amount": {"min": self._limits[0]},
                "cost": {"min": self._limits[1]},
            },
            "precision": {},
            "contractSize": 1.0,
        }


def test_min_notional_gate():
    ex = _LimEx((0.001, 5.0))
    assert meets_min_notional(ex, "X", 0.01, 100.0, True) is False  # 1 USDT < 5
    assert meets_min_notional(ex, "X", 0.1, 100.0, True) is True  # 10 USDT ok
    assert (
        meets_min_notional(_LimEx(fail=True), "X", 1.0, 100.0, True) is False
    )  # live: fail-safe
    assert (
        meets_min_notional(_LimEx(fail=True), "X", 1.0, 100.0, False) is True
    )  # paper: tolerant


def test_error_cooldown():
    c: dict = {}
    assert should_send_error(c, "k", 0.0) is True
    assert should_send_error(c, "k", 10.0) is False
    assert should_send_error(c, "k", 901.0) is True
    assert should_send_error(c, "other", 901.0) is True


def test_cached_equity_calls_once_per_ttl():
    _EQ_CACHE.clear()
    calls = []

    class FakeEx:
        @staticmethod
        def fetch_balance():
            calls.append(1)
            return {"total": {"USDT": 500.0}}

    assert cached_equity("e:S", FakeEx(), "S", "live", ttl=60.0) == 500.0
    assert cached_equity("e:S", FakeEx(), "S", "live", ttl=60.0) == 500.0
    assert len(calls) == 1


def test_save_open_only_on_change():
    from gg_bot.state import connect, load_open

    db = connect(":memory:")
    try:
        cache: dict = {}
        cyc = {"cycle": 1, "side": "long"}
        assert save_open_if_changed(db, "ex", "S", cyc, cache) is True
        assert save_open_if_changed(db, "ex", "S", cyc, cache) is False
        assert load_open(db, "ex", "S") == cyc
        cyc2 = {"cycle": 2, "side": "long"}
        assert save_open_if_changed(db, "ex", "S", cyc2, cache) is True
    finally:
        db.close()


def test_leverage_range_rejected():
    import pytest as _pt
    from gg_bot.config import SymbolCfg

    with _pt.raises(Exception):
        SymbolCfg(
            exchange="hyperliquid",
            symbol="BTC/USDC:USDC",
            asset_preset="BTC",
            timeframe="1h",
            leverage=126,
        )
    with _pt.raises(Exception):
        SymbolCfg(
            exchange="hyperliquid",
            symbol="BTC/USDC:USDC",
            asset_preset="BTC",
            timeframe="1h",
            leverage=0,
        )
    assert (
        SymbolCfg(
            exchange="hyperliquid",
            symbol="BTC/USDC:USDC",
            asset_preset="BTC",
            timeframe="1h",
            leverage=125,
        ).leverage
        == 125
    )


def test_backtest_tolerates_none_manual():
    from gg_bot.backtest import run

    n = 60
    px = 100 + np.cumsum(np.random.RandomState(7).randn(n))
    df = pd.DataFrame(
        {
            "open": px,
            "high": px + 0.5,
            "low": px - 0.5,
            "close": px,
            "volume": [10.0] * n,
        }
    )
    res = run(df, {}, {"bb_len": None, "sizing": None, "tp_weights": None})
    assert set(res) == {"trades", "winrate", "total_pct", "avg_pct"}


def test_swap_only_skips_broken_spot():
    from gg_bot.exchanges import _swap_only

    class FakeEx:
        @staticmethod
        def fetch_swap_markets(_params=None):
            return [{"symbol": "BTC/USDC:USDC"}]

        @staticmethod
        def fetch_spot_markets(_params=None):
            raise TypeError("unsupported operand type(s) for +: NoneType and str")

    ex = FakeEx()
    _swap_only(ex)
    assert ex.fetch_markets() == [{"symbol": "BTC/USDC:USDC"}]
