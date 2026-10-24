"""Configuration loading: YAML + env, with validation."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Literal
import os, yaml
from pydantic import BaseModel, Field, field_validator

class SymbolCfg(BaseModel):
    exchange: Literal["hyperliquid", "aster", "binance"]
    symbol: str  # e.g. BTC/USDT:USDT
    asset_preset: str = "BTC"   # Pine Asset Preset name
    timeframe: Literal["15m", "30m", "1h"] = "1h"
    leverage: int = 10
    order_mode: Literal["bot_managed", "exchange_native"] = "bot_managed"
    enabled: bool = True

class RiskCfg(BaseModel):
    max_daily_loss_pct: float = 5.0
    max_position_value_pct: float = 100.0
    kill_switch_on_start: bool = False
    cooldown_bars: int = 0

class TelegramCfg(BaseModel):
    enabled: bool = False
    notify_open: bool = True
    notify_close: bool = True
    notify_kill: bool = True
    notify_errors: bool = True

class AppCfg(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    symbols: list[SymbolCfg] = Field(default_factory=list)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    manual: dict[str, Any] = Field(default_factory=dict)  # overrides when preset == Manuell
    fine_tune: bool = False
    state_path: str = "/app/data/state.db"
    poll_seconds: int = 20

    @field_validator("symbols")
    @classmethod
    def need_symbols(cls, v):
        if not v:
            raise ValueError("config.symbols must not be empty")
        return v

def load_config(path: str | Path) -> AppCfg:
    p = Path(path)
    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8"))
    # env override for mode (Compose sets BOT_MODE)
    env_mode = os.getenv("BOT_MODE", "").strip().lower()
    if env_mode in ("paper", "live"):
        raw["mode"] = env_mode
    return AppCfg.model_validate(raw)

def redacted_dict(cfg: AppCfg) -> dict[str, Any]:
    d = cfg.model_dump()
    return d
