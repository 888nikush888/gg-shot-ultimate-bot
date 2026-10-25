"""SQLite persistence: cycles, kills, audit, open-cycle state."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from contextlib import suppress

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, exchange TEXT, symbol TEXT, side INTEGER, entry REAL, stop REAL, qty REAL, pnl REAL, note TEXT);
CREATE TABLE IF NOT EXISTS kills(ts TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS audit(ts TEXT, actor TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS open_cycles(exchange TEXT, symbol TEXT, payload TEXT, updated TEXT, PRIMARY KEY (exchange, symbol));
"""

def connect(path: str, audit_keep: int = 20000) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("DELETE FROM audit WHERE rowid NOT IN (SELECT rowid FROM audit ORDER BY rowid DESC LIMIT ?)", (audit_keep,))
    c.commit()
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

def list_open_cycles(c: sqlite3.Connection) -> list[dict]:
    rows = c.execute("SELECT exchange, symbol, payload, updated FROM open_cycles ORDER BY symbol").fetchall()
    out = []
    for e, s, pl, u in rows:
        try:
            payload = json.loads(pl)
        except Exception:
            payload = {}
        out.append({"exchange": e, "symbol": s, "payload": payload, "updated": u})
    return out

def kill_flag_path(state_path: str) -> Path:
    return Path(state_path).parent / "kill.flag"

def engage_kill_flag(state_path: str, reason: str) -> None:
    kill_flag_path(state_path).write_text(reason[:200], encoding="utf-8")

def clear_kill_flag(state_path: str) -> None:
    with suppress(FileNotFoundError):
        kill_flag_path(state_path).unlink()

def kill_flag_reason(state_path: str) -> str | None:
    try:
        return kill_flag_path(state_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
