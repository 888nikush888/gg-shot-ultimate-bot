"""SQLite persistence: cycles, fills, kills, audit."""
from __future__ import annotations
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, exchange TEXT, symbol TEXT, side INTEGER, entry REAL, stop REAL, qty REAL, pnl REAL, note TEXT);
CREATE TABLE IF NOT EXISTS kills(ts TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS audit(ts TEXT, actor TEXT, action TEXT, detail TEXT);
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
