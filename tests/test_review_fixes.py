import datetime as _dt
import pytest
from gg_bot.engine import (
    entry_prices,
    validate_manual,
    unknown_manual_keys,
    monitor,
    build_cycle,
)
from gg_bot.state import connect
from gg_bot.config import load_config
import os
import tempfile


def _cyc(**kw):
    man = {"tp_weights": [100.0], "fee_pct": 0.05}
    for k in ("trail_start",):
        if k in kw:
            man[k] = kw.pop(k)
    base = {
        "be_mode": "Aus",
        "be_buffer": 0.0,
        "trail_mode": "Prozent",
        "trail_atr": 3.0,
        "trail_pct": 1.0,
        "opposite_mode": "Ignorieren",
    }
    base.update(kw)
    return build_cycle(
        1,
        "long",
        1.0,
        100.0,
        97.0,
        {"tp_count": 1, "tp_basis": "Prozent", "tp1": 5.0, **base},
        man,
        1.0,
    )


def test_stop_modes():
    assert entry_prices(100.0, "long", "Prozent", {"stop_pct": 3.0}, 1.0) == 97.0
    assert entry_prices(100.0, "short", "ATR", {}, 2.0, {}) == 100.0 + 2.0 * 6.0
    ref = {"trend_line": 98.0, "tick": 0.1, "pad_ticks": 1}
    assert entry_prices(100.0, "long", "Trendlinie", {}, 1.0, ref) == 97.9
    ref2 = {"support": 95.0, "resistance": 110.0, "tick": 0.1, "pad_ticks": 2}
    assert entry_prices(100.0, "long", "S/R", {}, 1.0, ref2) == 94.8
    assert entry_prices(100.0, "short", "S/R", {}, 1.0, ref2) == 110.2
    auto = {"reverse": 96.0, "tick": 0.1, "pad_ticks": 1}
    assert (
        entry_prices(100.0, "long", "Automatisch", {"stop_pct": 3.0}, 1.0, auto) == 97.0
    )
    auto2 = {"reverse": 99.5, "tick": 0.1, "pad_ticks": 1}
    assert entry_prices(
        100.0, "long", "Automatisch", {"stop_pct": 3.0}, 1.0, auto2
    ) == pytest.approx(99.4)
    with pytest.raises(ValueError):
        entry_prices(100.0, "long", "Trendlinie", {}, 1.0)
    with pytest.raises(ValueError):
        entry_prices(100.0, "long", "S/R", {}, 1.0, {"tick": 0.1})
    with pytest.raises(ValueError):
        entry_prices(100.0, "long", "Nonsense", {}, 1.0, {})


def test_validate_manual():
    ok = validate_manual(
        {"legs": 1, "entry_mode": "Market", "tp_weights": [30.0, 70.0]}
    )
    assert ok["legs"] == 1
    for bad in [
        {"legs": 2},
        {"entry_mode": "Gestaffelt"},
        {"mode": "Classic"},
        {"tp_mode_2": "ATR-adaptiv"},
        {"tp_weights": []},
        {"tp_weights": [0, 0]},
        {"trail_start": "TP9"},
    ]:
        with pytest.raises(SystemExit):
            validate_manual(bad)


def test_password_pbkdf2_and_legacy(monkeypatch=None):
    import gg_bot.tui as tui

    h = tui.hash_password("geheim123")
    assert h.startswith("pbkdf2$")
    os.environ["DASHBOARD_PASSWORD_HASH"] = h
    try:
        assert tui.verify_password("geheim123") is True
        assert tui.verify_password("falsch") is False
        assert tui.verify_password("") is False
        import hashlib

        legacy = hashlib.sha256(("gg-shot-ultimate-v6:alt123").encode()).hexdigest()
        os.environ["DASHBOARD_PASSWORD_HASH"] = legacy
        assert tui.verify_password("alt123") is True
        assert tui.verify_password("falsch") is False
        os.environ["DASHBOARD_PASSWORD_HASH"] = "pbkdf2$nope"
        assert tui.verify_password("x") is False
    finally:
        del os.environ["DASHBOARD_PASSWORD_HASH"]
    assert tui.verify_password("x") is False


def test_monitor_trail_start_r():
    c = _cyc(trail_start="R")
    sig = {"long": False, "short": False, "osc_short": False, "osc_long": False}
    ev = monitor(c, 106.0, 106.0, 100.0, 1.0, 105.0, sig, {"trail_r": 1.0})
    assert ev["new_stop"] is not None and c["stop_reason"] == "TRAIL"
    c2 = _cyc(trail_start="TP4")
    ev2 = monitor(c2, 106.0, 106.0, 100.0, 1.0, 105.0, sig, {"trail_r": 1.0})
    assert ev2["new_stop"] is None


def test_unknown_manual_keys():
    assert "tp_weight" in unknown_manual_keys({"tp_weight": [1], "legs": 1})
    assert unknown_manual_keys({"legs": 1, "mode": "Standard"}) == []


def test_session_zone_day_boundary():
    from gg_bot.strategy import session_allows

    utc = _dt.timezone.utc
    now = _dt.datetime(2026, 1, 7, 0, 30, tzinfo=utc)  # Mittwoch 00:30 UTC
    assert (
        session_allows(
            now,
            {
                "session_on": True,
                "trade_session": "0000-0100",
                "session_zone": "Etc/UTC",
            },
        )
        is True
    )
    assert (
        session_allows(
            now,
            {
                "session_on": True,
                "trade_session": "0000-0100",
                "session_zone": "Pacific/Kiritimati",
            },
        )
        is False
    )


def test_audit_retention(tmp_path_factory=None):

    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    c = connect(p, audit_keep=2)
    for i in range(3):
        c.execute("INSERT INTO audit VALUES (?,?,?,?)", ("t", "a", "x", str(i)))
    c.commit()
    c.close()
    c2 = connect(p, audit_keep=2)
    assert c2.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 2
    c2.close()


def test_save_config_roundtrip():

    d = tempfile.mkdtemp()
    src = open("config/config.example.yaml", encoding="utf-8").read()
    p = os.path.join(d, "config.yaml")
    open(p, "w", encoding="utf-8").write(src)
    cfg = load_config(p)
    cfg.poll_seconds = 21
    from gg_bot.config import save_config as _sc

    bak = _sc(cfg, p)
    assert bak.exists() and load_config(p).poll_seconds == 21


def test_leverage_range():
    from gg_bot.config import SymbolCfg

    base = dict(
        exchange="hyperliquid",
        symbol="BTC/USDC:USDC",
        asset_preset="BTC",
        timeframe="1h",
    )
    assert SymbolCfg(**base, leverage=125).leverage == 125
    with pytest.raises(Exception):
        SymbolCfg(**base, leverage=126)
