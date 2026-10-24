"""Minimal historical backtest on CCXT OHLCV (parity smoke, not a full Pine replay)."""
from __future__ import annotations
import pandas as pd
from . import strategy as S

def run(df: pd.DataFrame, preset: dict, manual: dict, fee_pct: float = 0.05, slippage_pct: float = 0.0) -> dict:
    f = S.compute_frame(df, {**preset, **manual})
    f = S.trend_signals(f, {**preset, **manual})
    trades = []
    pos = None
    for i in range(len(f)):
        row = f.iloc[i]
        if pos is None and (row["turn_long"] or row["turn_short"]):
            side = 1 if row["turn_long"] else -1
            entry = row["close"] * (1 + slippage_pct / 100)
            stop = entry * (1 - side * float(preset.get("stop_pct", 3.0)) / 100)
            tps = S.tp_distances(entry, stop, row["atr"] if pd.notna(row["atr"]) else 0.0, preset, int(preset.get("tp_count", 4)))
            pos = {"side": side, "entry": entry, "stop": stop, "tp": entry + side * tps[0]}
        elif pos is not None:
            hi, lo = row["high"], row["low"]
            hit_tp = (hi >= pos["tp"]) if pos["side"] == 1 else (lo <= pos["tp"])
            hit_sl = (lo <= pos["stop"]) if pos["side"] == 1 else (hi >= pos["stop"])
            if hit_tp or hit_sl:
                px = pos["tp"] if hit_tp else pos["stop"]
                pnl = pos["side"] * (px - pos["entry"]) / pos["entry"] * 100 - fee_pct * 2
                trades.append(pnl)
                pos = None
    import numpy as np
    arr = np.array(trades, dtype=float)
    return {"trades": len(arr), "winrate": float((arr > 0).mean() * 100) if len(arr) else 0.0,
            "total_pct": float(arr.sum()) if len(arr) else 0.0, "avg_pct": float(arr.mean()) if len(arr) else 0.0}
