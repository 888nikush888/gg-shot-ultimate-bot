import inspect
import pandas as pd
import typer
from gg_bot.orders import stop_market_type
from gg_bot.engine import send_market_close, closed_frame, meets_min_notional
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
    assert not isinstance(default, bool), "Option im Funktionskoerper statt in Parametern"
    assert "--i-understand-live" in (default.param_decls or ())

def test_env_example_has_binance_keys():
    txt = open(".env.example", encoding="utf-8").read()
    assert "BINANCE_API_KEY=" in txt and "BINANCE_API_SECRET=" in txt


class _LimEx:
    def __init__(self, limits=None, fail=False):
        self._limits = limits
        self._fail = fail
    def load_markets(self):
        if self._fail:
            raise RuntimeError("offline")
    def market(self, symbol):
        return {"limits": {"amount": {"min": self._limits[0]}, "cost": {"min": self._limits[1]}},
                "precision": {}, "contractSize": 1.0}

def test_min_notional_gate():
    ex = _LimEx((0.001, 5.0))
    assert meets_min_notional(ex, "X", 0.01, 100.0, True) is False   # 1 USDT < 5
    assert meets_min_notional(ex, "X", 0.1, 100.0, True) is True    # 10 USDT ok
    assert meets_min_notional(_LimEx(fail=True), "X", 1.0, 100.0, True) is False  # live: fail-safe
    assert meets_min_notional(_LimEx(fail=True), "X", 1.0, 100.0, False) is True  # paper: tolerant
