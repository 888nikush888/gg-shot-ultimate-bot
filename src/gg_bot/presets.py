"""Preset resolution: presets.json (transcribed Pine f_cfg_* tables) + finetune."""
from __future__ import annotations
import json
from pathlib import Path
from functools import lru_cache

PRESETS_PATH = Path(__file__).resolve().parents[2] / "config" / "presets.json"

@lru_cache(maxsize=1)
def load_presets(path: str | Path | None = None) -> dict:
    p = Path(path) if path else PRESETS_PATH
    # fallback for running from /app where package is at /app/src
    if not p.exists():
        alt = Path("/app/config/presets.json")
        if alt.exists():
            p = alt
    return json.loads(p.read_text(encoding="utf-8"))

def preset_key(asset: str, timeframe: str) -> str:
    if asset == "Manuell":
        return "Manuell"
    return f"{asset}_{timeframe}"

def get_preset(asset: str, timeframe: str, fine_tune: bool = False, path=None) -> dict:
    db = load_presets(path)
    key = preset_key(asset, timeframe)
    if key == "Manuell":
        raise KeyError("Manuell preset must come from config.manual overrides")
    if key not in db:
        # fallback chain: same asset other TF -> Universal same TF -> error
        uni = f"Universal_{timeframe}"
        if uni in db:
            return db[uni]
        raise KeyError(f"unknown preset {key}")
    base = dict(db[key])
    if fine_tune:
        base = dict(base)
        base.update(db.get("_finetune_overrides", {}).get(key, {}))
    return base

def list_presets(path=None) -> list[str]:
    db = load_presets(path)
    return sorted(k for k in db.keys() if not k.startswith("_"))
