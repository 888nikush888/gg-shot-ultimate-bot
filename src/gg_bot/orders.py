"""Order execution: bot-managed vs exchange-native, idempotent client IDs."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class LegOrder:
    cycle: int
    leg: int
    qty: float
    price: float | None  # None = market
    side: str            # long | short

def client_id(cycle: int, leg: int, tranche: int) -> str:
    return f"gg{cycle}L{leg}T{tranche}"

def place_entry(ex, symbol: str, side: str, qty: float, price: float | None, cid: str) -> dict:
    otype = "market" if price is None else "limit"
    return ex.create_order(symbol, otype, "buy" if side == "long" else "sell", qty, price, {"clientOrderId": cid})

def place_stop_tp(ex, symbol: str, side: str, qty: float, stop: float | None, tp: float | None, cid: str, reduce_only: bool = True) -> list[dict]:
    outs = []
    params = {"reduceOnly": reduce_only, "clientOrderId": cid}
    exit_side = "sell" if side == "long" else "buy"
    if stop is not None:
        outs.append(ex.create_order(symbol, "stopMarket", exit_side, qty, None, {**params, "stopPrice": stop}))
    if tp is not None:
        outs.append(ex.create_order(symbol, "limit", exit_side, qty, tp, params))
    return outs

def close_all(ex, symbol: str, side: str | None = None) -> dict:
    pos = ex.fetch_positions([symbol])
    closed = []
    for p in pos:
        amt = float(p.get("contracts") or 0)
        if amt > 0:
            s = p.get("side")
            if side and s != side:
                continue
            closed.append(ex.create_order(symbol, "market", "sell" if s == "long" or (side == "long") else "buy", amt, None, {"reduceOnly": True}))
    return {"closed": len(closed)}
