"""CCXT factory for hyperliquid + aster + binance (perps). Paper mode works keyless."""

from __future__ import annotations
from contextlib import suppress
import os


def build(name: str, testnet: bool = False, need_auth: bool = True):
    import ccxt

    if name == "hyperliquid":
        wallet = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "")
        key = os.getenv("HYPERLIQUID_PRIVATE_KEY", "")
        params = {}
        if wallet and key:
            params = {"walletAddress": wallet, "privateKey": key}
        elif need_auth:
            raise RuntimeError(
                "HYPERLIQUID_WALLET_ADDRESS / HYPERLIQUID_PRIVATE_KEY missing in .env"
            )
        ex = ccxt.hyperliquid(params)
        _swap_only(ex)  # CCXT-Spot-Listing stuerzt ab (base=None); wir traden nur Perps
        if testnet:
            ex.set_sandbox_mode(True)
        return ex
    if name == "aster":
        key = os.getenv("ASTER_API_KEY", "")
        sec = os.getenv("ASTER_API_SECRET", "")
        params: dict = {"options": {"defaultType": "swap"}}
        if key and sec:
            params.update({"apiKey": key, "secret": sec})
        elif need_auth:
            raise RuntimeError("ASTER_API_KEY / ASTER_API_SECRET missing in .env")
        ex = ccxt.aster(params)
        if testnet:
            ex.set_sandbox_mode(True)
        return ex
    if name == "binance":
        key = os.getenv("BINANCE_API_KEY", "")
        sec = os.getenv("BINANCE_API_SECRET", "")
        params = {"options": {"defaultType": "swap"}}
        if key and sec:
            params.update({"apiKey": key, "secret": sec})
        elif need_auth:
            raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET missing in .env")
        ex = ccxt.binanceusdm(params)
        if testnet:
            ex.set_sandbox_mode(True)
        return ex
    raise ValueError(f"unknown exchange {name}")


def _swap_only(ex) -> None:
    """Laedt nur Perp-Maerkte (umgeht fehlerhafte Spot-Listings in CCXT)."""
    swap = ex.fetch_swap_markets
    ex.fetch_markets = lambda params={}: swap(params)  # noqa: E731


def set_leverage(ex, symbol: str, leverage: int) -> bool:
    """Push account leverage for symbol. Returns True if accepted."""
    try:
        ex.set_leverage(leverage, symbol)
        return True
    except Exception:
        try:
            ex.set_leverage(leverage, symbol, {"marginMode": "cross"})
            return True
        except Exception:
            return False


def fetch_position_qty(ex, symbol: str) -> float:
    """Signed open contracts for symbol (0 when flat). Never raises."""
    with suppress(Exception):
        for p in ex.fetch_positions([symbol]):
            if p.get("symbol") == symbol or symbol in str(p.get("symbol")):
                amt = float(p.get("contracts") or 0)
                if amt:
                    return amt if p.get("side") == "long" else -amt
    return 0.0


def price_tick(ex, symbol: str) -> float | None:
    """Kleinste Preisschrittweite aus Market-Precision (fuer Trendlinien-/S-R-Stops)."""
    try:
        ex.load_markets()
        prec = (ex.market(symbol).get("precision") or {}).get("price")
        return 10.0 ** (-int(prec))
    except Exception:
        return None


def market_limits(ex, symbol: str) -> dict:
    ex.load_markets()
    m = ex.market(symbol)
    return {
        "min_amount": m.get("limits", {}).get("amount", {}).get("min"),
        "min_cost": m.get("limits", {}).get("cost", {}).get("min"),
        "precision": m.get("precision", {}),
        "contract_size": m.get("contractSize", 1.0),
    }
