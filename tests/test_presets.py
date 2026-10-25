import json
import pathlib


def load():
    return json.loads(pathlib.Path("config/presets.json").read_text(encoding="utf-8"))


def test_preset_count_and_schema():
    db = load()
    keys = [k for k in db if not k.startswith("_")]
    assert len(keys) >= 70
    for k in ["BTC_1h", "ETH_1h", "SOL_15m", "Universal_1h"]:
        assert k in db
        p = db[k]
        for f in [
            "basis",
            "style",
            "bb_len",
            "filter_mode",
            "stop_mode",
            "tp_count",
            "tp_basis",
            "be_mode",
            "trail_mode",
        ]:
            assert f in p, (k, f)
        tps = [p["tp1"], p["tp2"], p["tp3"], p["tp4"]][: int(p["tp_count"])]
        assert tps == sorted(tps), k
        assert len(set(tps)) == len(tps), k


def test_finetune_overrides_exist():
    db = load()
    assert db["_finetune_overrides"]["BTC_1h"]["volume_min"] == 1.10


def test_finetune_golden_combo_group_a():
    db = load()
    ov = db["_finetune_overrides"]
    keys = [
        "BTC_1h",
        "NEAR_1h",
        "PEPE_1h",
        "SHIB_1h",
        "DOT_1h",
        "LINK_1h",
        "ETC_1h",
        "LTC_1h",
    ]
    for k in keys:
        assert k in ov, "Override fehlt: " + k
        assert ov[k]["stop_pct"] == 4.5
        assert ov[k]["trail_pct"] == 1.4
        assert ov[k]["volume_on"] is True
        assert ov[k]["volume_min"] == 1.1
        assert ov[k]["be_buffer"] == 0.2


def test_get_preset_resolution_and_isolation():
    from gg_bot.presets import get_preset

    base_btc = get_preset("BTC", "1h", fine_tune=False)
    assert base_btc["stop_pct"] == 4.0
    assert base_btc["volume_on"] is False
    gold_btc = get_preset("BTC", "1h", fine_tune=True)
    assert gold_btc["stop_pct"] == 4.5
    assert gold_btc["trail_pct"] == 1.4
    assert gold_btc["volume_on"] is True
    gold_near = get_preset("NEAR", "1h", fine_tune=True)
    assert gold_near["stop_pct"] == 4.5
    assert gold_near["trail_pct"] == 1.4
    sui_ft = get_preset("SUI", "1h", fine_tune=True)
    sui_base = get_preset("SUI", "1h", fine_tune=False)
    assert sui_ft["stop_pct"] == sui_base["stop_pct"]
    assert sui_ft["trail_pct"] == sui_base["trail_pct"]
