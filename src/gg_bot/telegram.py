"""Telegram notifications (trade open/close, kill, errors, daily)."""
from __future__ import annotations
import os
import httpx

def send(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", ""); chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text[:4000]}, timeout=15)
        return r.status_code == 200
    except Exception:
        return False

def trade_open(exchange: str, symbol: str, side: str, qty: float, price: float, stop: float) -> None:
    send(f"OPEN {exchange} {symbol} {side} qty={qty} entry~{price} SL={stop}")

def trade_close(exchange: str, symbol: str, pnl: float, note: str = "") -> None:
    send(f"CLOSE {exchange} {symbol} pnl={pnl:.2f} {note}")

def killed(reason: str) -> None:
    send(f"KILL-SWITCH ENGAGED: {reason}")
