import pandas as pd
from gg_bot import strategy as S
from gg_bot import indicators as I
from gg_bot.engine import settle_currency, build_cycle, monitor, get_equity
from gg_bot import exchanges as X
from gg_bot import orders as O


def test_settle_currency():
    assert settle_currency("BTC/USDC:USDC") == "USDC"
    assert settle_currency("BTC/USDT:USDT") == "USDT"
    assert settle_currency("ETH/USDT") == "USDT"


def test_build_cycle_splits_by_weight():
    c = build_cycle(
        7,
        "long",
        1.0,
        100.0,
        97.0,
        {
            "tp_count": 4,
            "tp_basis": "Prozent",
            "tp1": 2.0,
            "tp2": 4.0,
            "tp3": 6.0,
            "tp4": 12.0,
            "be_mode": "Aus",
            "be_buffer": 0.0,
            "trail_mode": "Aus",
            "trail_atr": 3.0,
            "trail_pct": 1.0,
            "opposite_mode": "Ignorieren",
        },
        {"tp_weights": [30, 25, 15, 10], "fee_pct": 0.05},
        1.0,
    )
    qs = [t["qty"] for t in c["tps"]]
    assert abs(sum(qs) - 1.0) < 1e-9 and len(qs) == 4
    assert abs(qs[0] - 0.375) < 1e-9 and abs(qs[1] - 0.3125) < 1e-9


def test_adaptive_smooth_differs_from_sharp():
    import numpy as np

    n = 300
    px = pd.Series(np.linspace(100, 130, n) + np.sin(np.arange(n)) * 0.5)
    reg = pd.Series([0.5] * n)
    ad = I.adaptive_series(px, reg, 3)
    assert ad.iloc[-1] == ad.iloc[-1]  # not NaN at end
    assert abs(ad.iloc[-1] - px.iloc[-1]) > 1e-9  # smoothed lags raw
    assert S._refs("Smooth", 1.0, 2.0, 1.1, 2.1, 1.5, 2.5) == (1.5, 2.5)
    assert S._refs("Sharp", 1.0, 2.0, 1.1, 2.1, 1.5, 2.5) == (1.0, 2.0)
    assert S._refs("Balanced", 1.0, 2.0, 1.1, 2.1, 1.5, 2.5) == (1.1, 2.1)


PRESET = {
    "tp_count": 4,
    "tp_basis": "Prozent",
    "tp1": 2.0,
    "tp2": 4.0,
    "tp3": 6.0,
    "tp4": 12.0,
    "be_mode": "TP1",
    "be_buffer": 0.1,
    "trail_mode": "Prozent",
    "trail_atr": 3.0,
    "trail_pct": 1.0,
    "opposite_mode": "Schliessen und drehen",
}


def test_build_cycle_tps_ascending_and_sum():
    c = build_cycle(1, "long", 1.0, 100.0, 97.0, PRESET, {}, 1.0)
    prices = [t["price"] for t in c["tps"]]
    assert prices == sorted(prices)
    assert abs(sum(t["qty"] for t in c["tps"]) - 1.0) < 1e-9


def test_monitor_sl_closes_all():
    c = build_cycle(1, "long", 1.0, 100.0, 97.0, PRESET, {}, 1.0)
    ev = monitor(
        c,
        96.0,
        96.5,
        95.0,
        1.0,
        99.0,
        {"long": False, "short": False, "osc_short": False, "osc_long": False},
        {},
    )
    assert ev["closed_all"] and ev["why"] == "SL"


def test_monitor_tp_then_be():
    c = build_cycle(1, "long", 1.0, 100.0, 97.0, PRESET, {}, 1.0)
    tp1 = c["tps"][0]["price"]
    ev = monitor(
        c,
        tp1,
        tp1,
        99.0,
        1.0,
        101.0,
        {"long": False, "short": False, "osc_short": False, "osc_long": False},
        {},
    )
    assert c["tps"][0]["hit"] and not ev["closed_all"]
    assert c["stop"] > 100.0 and c["stop_reason"] in ("BE", "TRAIL")


def test_monitor_opposite_close_and_reverse():
    c = build_cycle(1, "long", 1.0, 100.0, 97.0, PRESET, {}, 1.0)
    ev = monitor(
        c,
        100.0,
        101.0,
        99.0,
        1.0,
        100.5,
        {"long": False, "short": True, "osc_short": False, "osc_long": False},
        {},
    )
    assert ev["closed_all"] and ev["why"] == "OPP_REVERSE"


def test_monitor_opposite_ignore():
    p = dict(PRESET, opposite_mode="Ignorieren")
    c = build_cycle(1, "long", 1.0, 100.0, 97.0, p, {}, 1.0)
    ev = monitor(
        c,
        100.0,
        101.0,
        99.0,
        1.0,
        100.5,
        {"long": False, "short": True, "osc_short": False, "osc_long": False},
        {},
    )
    assert not ev["closed_all"]


def test_get_equity_raises_without_balance():
    class FakeEx:
        @staticmethod
        def fetch_balance():
            return {"total": {}}

    try:
        get_equity(FakeEx(), "BTC/USDC:USDC", "live")
        assert False, "must raise"
    except RuntimeError:
        pass


def test_get_equity_uses_settle():
    class FakeEx:
        @staticmethod
        def fetch_balance():
            return {"total": {"USDT": 0, "USDC": 500.0}}

    assert get_equity(FakeEx(), "BTC/USDC:USDC", "live") == 500.0


def test_set_leverage_passthrough():
    calls = []

    class FakeEx:
        @staticmethod
        def set_leverage(lev, sym, _params=None):
            calls.append((lev, sym))

    assert X.set_leverage(FakeEx(), "BTC/USDC:USDC", 10) is True
    assert calls == [(10, "BTC/USDC:USDC")]


def test_fetch_position_qty_signed():
    class FakeEx:
        @staticmethod
        def fetch_positions(_syms):
            return [
                {"symbol": "BTC/USDC:USDC", "contracts": 2.0, "side": "long"},
                {"symbol": "BTC/USDC:USDC", "contracts": 1.0, "side": "short"},
            ]

    assert X.fetch_position_qty(FakeEx(), "BTC/USDC:USDC") == 2.0


def test_close_all_sides():
    made = []

    class FakeEx:
        @staticmethod
        def fetch_positions(_syms):
            return [{"symbol": "X", "contracts": 3.0, "side": "short"}]

        @staticmethod
        def create_order(_sym, _typ, side, _amt, _px, _params):
            made.append(side)
            return {}

    assert O.close_all(FakeEx(), "X")["closed"] == 1
    assert made == ["buy"]


def test_preset_count_is_78():
    import json

    db = json.load(open("config/presets.json", encoding="utf-8"))
    keys = [k for k in db if not k.startswith("_")]
    assert len(keys) == 78
    assert db["_finetune_overrides"]["BTC_1h"]["volume_min"] == 1.10
