import inspect
import pandas as pd
import typer
from gg_bot.orders import stop_market_type
from gg_bot.engine import send_market_close, closed_frame
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
