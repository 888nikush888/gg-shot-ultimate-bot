"""Position sizing exactly like the Pine validSetup block."""
from __future__ import annotations
import math

def plan_qty(equity: float, avg_plan: float, point_value: float, sizing: str,
             risk_pct: float, capital_pct: float, leverage: float, exposure_cap_pct: float,
             stop_dist_per_unit: float, weights: list[float], legs: int, tp_count: int,
             lot: float) -> tuple[list[float], float, float, str]:
    if equity <= 0 or avg_plan <= 0:
        return [], 0.0, 0.0, "equity/price invalid"
    max_position_value = equity * min(exposure_cap_pct / 100.0, leverage)
    cap_qty = max_position_value / (avg_plan * point_value)
    if sizing == "Stoprisiko":
        if stop_dist_per_unit <= 0:
            return [], 0.0, max_position_value, "stop distance invalid"
        requested = equity * risk_pct / 100.0 / stop_dist_per_unit
    else:
        requested = equity * capital_pct / 100.0 * leverage / (avg_plan * point_value)
    total_qty = min(requested, cap_qty)
    total_lots = int(math.floor(total_qty / lot + 1e-9))
    if total_lots < legs * tp_count:
        return [], 0.0, max_position_value, "total lots below legs*tranches minimum"
    wsum = sum(weights[:legs])
    lots_left = total_lots
    per_leg_lots: list[int] = []
    for i in range(legs):
        if i == legs - 1:
            per_leg_lots.append(lots_left)
        else:
            wanted = int(math.floor(total_lots * weights[i] / wsum))
            alloc = min(max(tp_count, wanted), lots_left - (legs - i - 1) * tp_count)
            per_leg_lots.append(alloc)
            lots_left -= alloc
    return [x * lot for x in per_leg_lots], total_qty, max_position_value, ""

def split_tp_qty(total_qty: float, weights: list[float], tp_count: int, lot: float) -> list[float]:
    """Split whole position across TP tranches by weight (floor + remainder to last)."""
    w = weights[:tp_count]
    wsum = sum(w)
    total_lots = int(math.floor(total_qty / lot + 1e-9))
    if total_lots < tp_count:
        raise ValueError("qty below tranche minimum")
    out_lots, left = [], total_lots
    for j in range(tp_count):
        if j == tp_count - 1:
            out_lots.append(left)
        else:
            q = max(1, int(math.floor(total_lots * w[j] / wsum)))
            q = min(q, left - (tp_count - j - 1))
            out_lots.append(q)
            left -= q
    return [x * lot for x in out_lots]
