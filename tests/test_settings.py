import numpy as np
import pandas as pd
from gg_bot import strategy as S
from gg_bot.strategy import pending_state
from gg_bot.engine import should_resize_native_stop, pullback_anchor, evaluate
from gg_bot.presets import get_preset


def _frame(
    turn_long=False,
    turn_short=False,
    bounce_long=False,
    bounce_short=False,
    osc_up=False,
    osc_dn=False,
    bull_age=float("inf"),
    bear_age=float("inf"),
    trend_no=3,
):
    n = 300
    return pd.DataFrame(
        {
            "turn_long": [False] * (n - 1) + [turn_long],
            "turn_short": [False] * (n - 1) + [turn_short],
            "bounce_long": [False] * (n - 1) + [bounce_long],
            "bounce_short": [False] * (n - 1) + [bounce_short],
            "osc_cross_up": [False] * (n - 1) + [osc_up],
            "osc_cross_dn": [False] * (n - 1) + [osc_dn],
            "bull_div_age": [9999.0] * (n - 1) + [bull_age],
            "bear_div_age": [9999.0] * (n - 1) + [bear_age],
            "trend_no": [3] * (n - 1) + [trend_no],
            "trend_dir": [1] * n,
            "f_atr": [1.0] * n,
            "f_avg": [0.5] * n,
            "rsi": [50.0] * n,
            "volume": [10.0] * n,
            "vol_avg": [10.0] * n,
            "close": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "atr": [1.0] * n,
            "trend_line": [99.0] * n,
        }
    )


def _eval(frame, manual, trend_entries=0, preset=None):
    orig = S.trend_signals
    S.trend_signals = lambda df, p: frame
    try:
        return evaluate(frame, preset or {}, manual, trend_entries)
    finally:
        S.trend_signals = orig


BASE = {"filter_mode": "Aus"}


def test_turn_signals_fire():
    assert _eval(_frame(turn_long=True), BASE)["long"] is True
    assert _eval(_frame(turn_short=True), BASE)["short"] is True


def test_allow_long_short_gates():
    assert _eval(_frame(turn_long=True), {"allow_long": False})["long"] is False
    assert _eval(_frame(turn_short=True), {"allow_short": False})["short"] is False


def test_div_filter():
    m = {"div_mode": "Filter", "div_age": 20}
    assert _eval(_frame(turn_long=True), m)["long"] is False
    assert _eval(_frame(turn_long=True, bull_age=5), m)["long"] is True
    assert _eval(_frame(turn_short=True, bear_age=5), m)["short"] is True


def test_osc_entries_and_reentry():
    assert (
        _eval(
            _frame(osc_up=True), {"osc_entries": True, "osc_mode": "Beide Richtungen"}
        )["long"]
        is True
    )
    assert (
        _eval(_frame(osc_up=True), {"osc_entries": True, "osc_mode": "Aus"})["long"]
        is False
    )
    m = {"mode": "Advanced", "reentry_on": False}
    assert _eval(_frame(bounce_long=True), m, trend_entries=1)["long"] is False
    assert _eval(_frame(bounce_long=True), m, trend_entries=0)["long"] is True
    m2 = {"mode": "Advanced", "reentry_on": True, "max_reentries": 1}
    assert _eval(_frame(bounce_long=True), m2, trend_entries=1)["long"] is True
    assert _eval(_frame(bounce_long=True), m2, trend_entries=2)["long"] is False


def test_session_gate():
    assert _eval(_frame(turn_long=True), {})["session_ok"] is True
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    out_h = (now.hour + 3) % 24
    sess = f"{out_h:02d}00-{out_h:02d}30"
    assert S.session_allows(now, {"session_on": True, "trade_session": sess}) is False
    assert (
        S.session_allows(now, {"session_on": True, "trade_session": "0000-0000"})
        is True
    )
    assert S.session_allows(now, {"session_on": False}) is True
    assert S.date_allows(now, {}) is True
    assert S.date_allows(now, {"date_start": "2099-01-01"}) is False


def test_pending_state():
    assert pending_state(True, 1.0, False, 0, 100) == "filled"
    assert pending_state(True, 0.0, False, 0, 100) == "waiting"
    assert pending_state(True, 0.0, False, 100, 100) == "expired"
    assert pending_state(False, 0.0, True, 0, 100) == "filled"
    assert pending_state(False, 0.0, False, 0, 100) == "waiting"


def test_pullback_anchor_and_resize():
    assert (
        pullback_anchor(
            100.0, "long", 2.0, {"entry_unit": "ATR", "pullback_distance": 0.5}
        )
        == 99.0
    )
    assert (
        pullback_anchor(
            100.0, "short", 2.0, {"entry_unit": "Prozent", "pullback_distance": 1.0}
        )
        == 101.0
    )
    assert should_resize_native_stop("exchange_native", True, [(1, 2, "TP")]) is True
    assert should_resize_native_stop("exchange_native", True, [(1, 2, "OPP")]) is False
    assert should_resize_native_stop("bot_managed", True, [(1, 2, "TP")]) is False
    assert should_resize_native_stop("exchange_native", False, [(1, 2, "TP")]) is False


def test_manuell_defaults_and_aliases():
    p = get_preset("Manuell", "1h")
    assert p["basis"]
    assert p["stop_mode"]
    assert p["be_mode"]
    m = S.normalize_manual(
        {"oscMode": "Beide Richtungen", "osc_mode": "Aus", "nonsense": 1}
    )
    assert m["osc_mode"] == "Aus"
    assert m["nonsense"] == 1


def test_div_supres_detects():
    df = pd.DataFrame(
        {
            "rsi": [30.0] * 20 + [40.0] * 20,
            "piv_low": [np.nan] * 40,
            "piv_high": [np.nan] * 40,
        }
    )
    df.loc[10, "piv_low"] = 90.0
    df.loc[30, "piv_low"] = 85.0
    bull, bear, sup, res = S.div_supres(df, {"pivot_len": 5})
    assert bull[30] == 0
    assert bull[39] == 9
    assert sup[39] == 85.0
    assert pd.isna(res[39])
    assert np.all(np.isinf(bear))
