"""CCXT factory for hyperliquid + aster (perps). Paper mode works keyless (public market data)."""
from __future__ import annotations
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
            raise RuntimeError("HYPERLIQUID_WALLET_ADDRESS / HYPERLIQUID_PRIVATE_KEY missing in .env")
        ex = ccxt.hyperliquid(params)
        if testnet:
            ex.set_sandbox_mode(True)
        return ex
    if name == "aster":
        key = os.getenv("ASTER_API_KEY", ""); sec = os.getenv("ASTER_API_SECRET", "")
        params: dict = {"options": {"defaultType": "swap"}}
        if key and sec:
            params.update({"apiKey": key, "secret": sec})
        elif need_auth:
            raise RuntimeError("ASTER_API_KEY / ASTER_API_SECRET missing in .env")
        ex = ccxt.aster(params)
        if testnet:
            ex.set_sandbox_mode(True)
        return ex
    raise ValueError(f"unknown exchange {name}")

def market_limits(ex, symbol: str) -> dict:
    ex.load_markets()
    m = ex.market(symbol)
    return {"min_amount": m.get("limits", {}).get("amount", {}).get("min"), "min_cost": m.get("limits", {}).get("cost", {}).get("min"), "precision": m.get("precision", {}), "contract_size": m.get("contractSize", 1.0)}
