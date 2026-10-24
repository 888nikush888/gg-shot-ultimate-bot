"""Order execution: entries, full TP/SL brackets, position close. Idempotent client IDs."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class LegOrder:
    cycle: int
    leg: int
    qty: float
    price: float | None
    side: str

def client_id(cycle: int, leg: int, tranche: int) -> str:
    return f"gg{cycle}L{leg}T{tranche}"


def stop_market_type(ex) -> str:
    """Exchange-specific stop-market order type. Binance USD-M needs uppercase."""
    return "STOP_MARKET" if "binance" in getattr(ex, "id", "") else "stopMarket"

def place_entry(ex, symbol: str, side: str, qty: float, price: float | None, cid: str) -> dict:
    otype = "market" if price is None else "limit"
    return ex.create_order(symbol, otype, "buy" if side == "long" else "sell", qty, price, {"clientOrderId": cid})

def place_bracket(ex, symbol: str, side: str, total_qty: float, stop: float,
                  tps: list[tuple[float, float]], cycle: int) -> list[dict]:
    """SL (full qty, stopMarket reduceOnly) + one limit TP per tranche (reduceOnly)."""
    exit_side = "sell" if side == "long" else "buy"
    outs = [ex.create_order(symbol, stop_market_type(ex), exit_side, total_qty, None,
                            {"stopPrice": stop, "reduceOnly": True,
                             "clientOrderId": client_id(cycle, 0, 8)})]
    for j, (px, q) in enumerate(tps):
        if q > 0:
            outs.append(ex.create_order(symbol, "limit", exit_side, q, px,
                                        {"reduceOnly": True, "clientOrderId": client_id(cycle, 0, j)}))
    return outs

def close_position(ex, symbol: str, side: str, qty: float, cid: str = "") -> dict | None:
    if qty <= 0:
        return None
    params: dict = {"reduceOnly": True}
    if cid:
        params["clientOrderId"] = cid
    return ex.create_order(symbol, "market", "sell" if side == "long" else "buy", qty, None, params)

def close_all(ex, symbol: str) -> dict:
    """Close every open position on symbol. Returns closed count + qtys."""
    closed, qtys = 0, []
    try:
        positions = ex.fetch_positions([symbol])
    except Exception:
        positions = []
    for p in positions:
        amt = abs(float(p.get("contracts") or 0))
        if amt <= 0:
            continue
        side = p.get("side") or ("long" if float(p.get("contracts") or 0) > 0 else "short")
        try:
            ex.create_order(symbol, "market", "sell" if side == "long" else "buy",
                            amt, None, {"reduceOnly": True})
            closed += 1
            qtys.append(amt)
        except Exception:
            continue
    return {"closed": closed, "qty": qtys}
