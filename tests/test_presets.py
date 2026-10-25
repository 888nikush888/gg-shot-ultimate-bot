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
        assert tps == sorted(tps) and len(set(tps)) == len(tps), k


def test_finetune_overrides_exist():
    db = load()
    assert db["_finetune_overrides"]["BTC_1h"]["volume_min"] == 1.10
