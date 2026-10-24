"""SQLite persistence: cycles, kills, audit, open-cycle state."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, exchange TEXT, symbol TEXT, side INTEGER, entry REAL, stop REAL, qty REAL, pnl REAL, note TEXT);
CREATE TABLE IF NOT EXISTS kills(ts TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS audit(ts TEXT, actor TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS open_cycles(exchange TEXT, symbol TEXT, payload TEXT, updated TEXT, PRIMARY KEY (exchange, symbol));
"""

def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    return c

def audit(c: sqlite3.Connection, actor: str, action: str, detail: str = "") -> None:
    import datetime
    c.execute("INSERT INTO audit VALUES (?,?,?,?)", (datetime.datetime.now(datetime.timezone.utc).isoformat(), actor, action, detail[:2000]))
    c.commit()

def save_open(c: sqlite3.Connection, exchange: str, symbol: str, payload: dict) -> None:
    import datetime
    c.execute("INSERT OR REPLACE INTO open_cycles VALUES (?,?,?,?)",
              (exchange, symbol, json.dumps(payload),
               datetime.datetime.now(datetime.timezone.utc).isoformat()))
    c.commit()

def load_open(c: sqlite3.Connection, exchange: str, symbol: str) -> dict | None:
    r = c.execute("SELECT payload FROM open_cycles WHERE exchange=? AND symbol=?", (exchange, symbol)).fetchone()
    return json.loads(r[0]) if r else None

def clear_open(c: sqlite3.Connection, exchange: str, symbol: str) -> None:
    c.execute("DELETE FROM open_cycles WHERE exchange=? AND symbol=?", (exchange, symbol))
    c.commit()

def list_open(c: sqlite3.Connection) -> list[dict]:
    return [{"exchange": e, "symbol": s} for e, s in c.execute("SELECT exchange, symbol FROM open_cycles")]
