"""Full CLI control: run/status/config/presets/backtest/mode/positions/kill/telegram."""
from __future__ import annotations
import json, sys
from pathlib import Path
import typer
from .logging_setup import setup_logging, get_logger
from .config import load_config

app = typer.Typer(add_completion=False, help="GG-Shot Ultimate v6 bot")
log = get_logger("cli")

@app.callback()
def _cb(verbose: bool = typer.Option(False, "--verbose")):
    setup_logging("DEBUG" if verbose else "INFO")

@app.command()
def run(config: str = typer.Option("/app/config/config.yaml", "--config"), once: bool = typer.Option(False, "--once")):
    """Start the engine (paper or live per config/BOT_MODE)."""
    cfg = load_config(config)
    if cfg.mode == "live":
        typer.echo("LIVE MODE - guardrails active (kill-switch, daily loss). Ctrl+C to stop.")
    from .engine import run_loop
    run_loop(cfg, once=once)

@app.command()
def healthcheck(config: str = typer.Option("/app/config/config.yaml", "--config")):
    """Docker HEALTHCHECK: config loads + presets parse."""
    try:
        cfg = load_config(config)
        from .presets import load_presets
        load_presets()
        typer.echo(json.dumps({"ok": True, "mode": cfg.mode, "symbols": len(cfg.symbols)}))
    except Exception as e:
        typer.echo(json.dumps({"ok": False, "error": str(e)}))
        raise typer.Exit(1)

@app.command()
def status(config: str = typer.Option("/app/config/config.yaml", "--config")):
    cfg = load_config(config)
    import sqlite3
    dbp = Path(cfg.state_path)
    n = 0
    if dbp.exists():
        c = sqlite3.connect(str(dbp))
        try:
            n = c.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        except Exception:
            n = 0
    typer.echo(json.dumps({"mode": cfg.mode, "symbols": [s.model_dump() for s in cfg.symbols], "cycles": n}, indent=2))

@app.command("config-validate")
def config_validate(config: str = typer.Option("/app/config/config.yaml", "--config"), check_markets: bool = typer.Option(False, "--check-markets")):
    cfg = load_config(config)
    from .presets import get_preset
    for s in cfg.symbols:
        get_preset(s.asset_preset, s.timeframe, cfg.fine_tune)
    if check_markets:
        from . import exchanges as X
        for s in cfg.symbols:
            if s.enabled:
                ex = X.build(s.exchange, need_auth=False)
                ex.load_markets()
                assert s.symbol in ex.symbols, f"{s.exchange} has no market {s.symbol}"
    typer.echo(f"OK: mode={cfg.mode} symbols={len(cfg.symbols)}")

@app.command("config-show")
def config_show(config: str = typer.Option("/app/config/config.yaml", "--config")):
    cfg = load_config(config)
    typer.echo(json.dumps(cfg.model_dump(), indent=2))

@app.command("preset-list")
def preset_list():
    from .presets import list_presets
    for k in list_presets():
        typer.echo(k)

@app.command("preset-show")
def preset_show(key: str):
    from .presets import load_presets
    db = load_presets()
    if key not in db:
        raise typer.Exit(f"unknown preset {key}")
    typer.echo(json.dumps(db[key], indent=2))

@app.command()
def backtest(config: str = typer.Option("/app/config/config.yaml", "--config"), symbol: str = typer.Option("", "--symbol"), limit: int = typer.Option(1000, "--limit")):
    """Fetch OHLCV via CCXT and run parity-smoke backtest (read-only, no orders)."""
    cfg = load_config(config)
    from .presets import get_preset
    from . import exchanges as X
    from .backtest import run as bt
    import pandas as pd
    target = next((s for s in cfg.symbols if (not symbol or s.symbol == symbol)), cfg.symbols[0])
    ex = X.build(target.exchange, need_auth=False)
    ohlcv = ex.fetch_ohlcv(target.symbol, target.timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    preset = get_preset(target.asset_preset, target.timeframe, cfg.fine_tune)
    res = bt(df, preset, cfg.manual)
    typer.echo(json.dumps({"symbol": target.symbol, **res}, indent=2))

@app.command("positions-close-all")
def positions_close_all(config: str = typer.Option("/app/config/config.yaml", "--config"),
                        symbol: str = typer.Option("", "--symbol"),
                        i_understand_live: bool = typer.Option(False, "--i-understand-live")):
    """Emergency: close all positions (reduceOnly market). Requires --i-understand-live flag in live."""
    cfg = load_config(config)
    if cfg.mode != "live":
        typer.echo("paper mode: nothing to close (no live orders)")
        return
    if not i_understand_live:
        raise typer.Exit("refusing live close-all without --i-understand-live")
    from . import exchanges as X
    from .orders import close_all
    for s in cfg.symbols:
        if symbol and s.symbol != symbol:
            continue
        ex = X.build(s.exchange)
        typer.echo(json.dumps({"symbol": s.symbol, **close_all(ex, s.symbol)}))

@app.command("kill-engage")
def kill_engage(config: str = typer.Option("/app/config/config.yaml", "--config"), reason: str = typer.Option("manual", "--reason")):
    from .state import connect, audit
    cfg = load_config(config)
    db = connect(cfg.state_path)
    import datetime
    db.execute("INSERT INTO kills VALUES (?,?)", (datetime.datetime.now(datetime.timezone.utc).isoformat(), reason))
    db.commit(); audit(db, "cli", "kill-engage", reason)
    from .state import engage_kill_flag
    engage_kill_flag(cfg.state_path, reason)
    from . import telegram as TG
    if cfg.telegram.enabled:
        TG.killed(reason)
    typer.echo(f"killed: {reason}")

@app.command("kill-reset")
def kill_reset(config: str = typer.Option("/app/config/config.yaml", "--config")):
    """Remove kill flag so entries resume (live still needs review)."""
    from .state import connect, audit, clear_kill_flag
    cfg = load_config(config)
    clear_kill_flag(cfg.state_path)
    audit(connect(cfg.state_path), "cli", "kill-reset", "")
    typer.echo("kill flag cleared")


@app.command("dashboard-hash-password")
def dashboard_hash_password():
    """Print a DASHBOARD_PASSWORD_HASH line for .env (paste it, never commit plaintext)."""
    import getpass
    from .tui import hash_password
    pw = getpass.getpass("dashboard password: ")
    again = getpass.getpass("repeat: ")
    if not pw or pw != again:
        raise typer.Exit("empty or mismatch, nothing printed")
    typer.echo("DASHBOARD_PASSWORD_HASH=" + hash_password(pw))


@app.command("dashboard")
def dashboard(config: str = typer.Option("/app/config/config.yaml", "--config")):
    """Open the interactive terminal dashboard (password gate via DASHBOARD_PASSWORD_HASH)."""
    import getpass
    from .tui import DashboardApp, verify_password
    for attempt in range(3):
        if verify_password(getpass.getpass("dashboard password: ")):
            break
    else:
        raise typer.Exit("access denied")
    DashboardApp(config).run()


@app.command("telegram-test")
def telegram_test():
    from . import telegram as TG
    ok = TG.send("GG-Bot Telegram-Test OK")
    typer.echo("sent" if ok else "not-configured-or-failed")
    if not ok:
        raise typer.Exit(1)

if __name__ == "__main__":
    app()
