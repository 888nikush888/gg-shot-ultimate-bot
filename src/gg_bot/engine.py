"""Main loop: one process, N symbols, paper/live, kill-switch, reconcile."""
from __future__ import annotations
import time
from datetime import datetime, timezone
import pandas as pd
from .config import AppCfg
from .presets import get_preset
from . import strategy as S
from .sizing import plan_qty
from .risk import RiskState
from .state import connect, audit
from . import exchanges as X
from . import telegram as TG
from .logging_setup import get_logger

log = get_logger("engine")

def fetch_frame(ex, symbol: str, tf: str, limit: int = 600) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def evaluate(df: pd.DataFrame, preset: dict, manual: dict) -> dict:
    p = {**preset, **{k: v for k, v in manual.items() if v is not None}}
    f = S.compute_frame(df, p)
    f = S.trend_signals(f, p, int(manual.get("pivot_len", 5) if manual else 5))
    last = f.iloc[-1]
    fm = p.get("filter_mode", "Aus")
    atr_tr = bool(last["f_atr"] >= last["f_avg"]) if pd.notna(last["f_atr"]) else False
    rsi_tr = bool(last["rsi"] > float(p.get("rsi_upper", 45)) or last["rsi"] < float(p.get("rsi_lower", 10)))
    flat = bool(last["rsi"] < float(p.get("rsi_upper", 45)) and last["rsi"] > float(p.get("rsi_lower", 10)))
    filt = {"Aus": True, "ATR": atr_tr, "RSI": rsi_tr, "ATR oder RSI": atr_tr or rsi_tr,
            "ATR und RSI": atr_tr and rsi_tr, "Seitwaerts: ATR oder RSI": (not atr_tr) or flat,
            "Seitwaerts: ATR und RSI": (not atr_tr) and flat}.get(fm, True)
    vol_ok = True
    if p.get("volume_on"):
        va = last.get("vol_avg", float("nan"))
        vol_ok = bool(pd.notna(va) and va > 0 and last["volume"] / va >= float(p.get("volume_min", 1.0)))
    want_long = bool(last["turn_long"]); want_short = bool(last["turn_short"])
    return {"long": want_long and filt and vol_ok, "short": want_short and filt and vol_ok,
            "close": float(last["close"]), "atr": float(last["atr"] if pd.notna(last["atr"]) else 0.0),
            "trend_line": float(last["trend_line"] if pd.notna(last["trend_line"]) else last["close"])}

def run_loop(cfg: AppCfg, once: bool = False) -> None:
    risk = RiskState(killed=cfg.risk.kill_switch_on_start, kill_reason="kill_switch_on_start" if cfg.risk.kill_switch_on_start else "")
    db = connect(cfg.state_path)
    ex_map = {}
    for s in cfg.symbols:
        if s.enabled and s.exchange not in ex_map:
            try:
                ex_map[s.exchange] = X.build(s.exchange, need_auth=(cfg.mode == "live"))  # live endpoints; paper = no orders
            except Exception as e:
                log.error("exchange init failed", extra={"exchange": s.exchange, "error": str(e)})
                raise
    audit(db, "engine", "start", f"mode={cfg.mode} symbols={len(cfg.symbols)}")
    if cfg.mode == "live":
        TG.send(f"Bot gestartet (LIVE): {len(cfg.symbols)} Symbole")
    cycle = 0
    while True:
        for s in cfg.symbols:
            if not s.enabled or risk.killed:
                continue
            ex = ex_map[s.exchange]
            try:
                key_asset = s.asset_preset
                preset = get_preset(key_asset, s.timeframe, cfg.fine_tune)
                df = fetch_frame(ex, s.symbol, s.timeframe)
                sig = evaluate(df, preset, cfg.manual)
                equity = float(ex.fetch_balance().get("total", {}).get("USDT", 10000)) if cfg.mode == "live" else 10000.0
                if cfg.mode == "paper":
                    if sig["long"] or sig["short"]:
                        log.info("paper signal", extra={"symbol": s.symbol, "side": "long" if sig["long"] else "short", "price": sig["close"]})
                        if cfg.telegram.enabled:
                            TG.trade_open(s.exchange, s.symbol, "long" if sig["long"] else "short", 0.0, sig["close"], 0.0)
                    continue
                # LIVE path
                if not (sig["long"] or sig["short"]):
                    continue
                side = "long" if sig["long"] else "short"
                stop_mode = preset.get("stop_mode", "Prozent")
                anchor = sig["close"]
                if stop_mode == "Prozent":
                    stop = anchor * (1 - (1 if side == "long" else -1) * float(preset.get("stop_pct", 3.0)) / 100)
                else:
                    stop = anchor - (1 if side == "long" else -1) * sig["atr"] * float(preset.get("stop_atr_mult", 6.0))
                avg_plan, pv = anchor, 1.0
                weights = [30, 25, 15, 10, 20][: max(1, int(cfg.manual.get("legs", 1) if cfg.manual else 1))]
                qtys, _, _, err = plan_qty(equity, avg_plan, pv, cfg.manual.get("sizing", "Stoprisiko") if cfg.manual else "Stoprisiko",
                                           float(cfg.manual.get("risk_pct", 1.0) if cfg.manual else 1.0),
                                           float(cfg.manual.get("capital_pct", 10.0) if cfg.manual else 10.0),
                                           s.leverage, cfg.risk.max_position_value_pct,
                                           abs(avg_plan - stop) * pv, weights, len(weights), int(preset.get("tp_count", 4)), 0.001)
                if err:
                    log.error("sizing blocked", extra={"symbol": s.symbol, "error": err})
                    continue
                cycle += 1
                from .orders import place_entry, client_id
                if s.order_mode == "bot_managed":
                    place_entry(ex, s.symbol, side, sum(qtys), None, client_id(cycle, 0, 9))
                else:
                    tps = S.tp_distances(anchor, stop, sig["atr"], preset, int(preset.get("tp_count", 4)))
                    tp1 = anchor + (1 if side == "long" else -1) * tps[0]
                    from .orders import place_stop_tp
                    oid = place_entry(ex, s.symbol, side, sum(qtys), None, client_id(cycle, 0, 9))
                    place_stop_tp(ex, s.symbol, side, sum(qtys), stop, tp1, client_id(cycle, 0, 0))
                db.execute("INSERT INTO cycles(ts,exchange,symbol,side,entry,stop,qty,pnl,note) VALUES (?,?,?,?,?,?,?,?,?)",
                           (datetime.now(timezone.utc).isoformat(), s.exchange, s.symbol, 1 if side == "long" else -1, anchor, stop, sum(qtys), 0.0, f"cycle={cycle}"))
                db.commit()
                audit(db, "engine", "entry", f"{s.exchange} {s.symbol} {side} qty={sum(qtys)}")
                if cfg.telegram.enabled:
                    TG.trade_open(s.exchange, s.symbol, side, sum(qtys), anchor, stop)
            except Exception as e:
                log.error("symbol loop error", extra={"symbol": s.symbol, "error": str(e)})
                if cfg.telegram.enabled and cfg.telegram.notify_errors:
                    TG.send(f"FEHLER {s.exchange} {s.symbol}: {e}")
        if once:
            break
        time.sleep(max(5, cfg.poll_seconds))
