"""Main loop: one process, N symbols, paper/live, leverage, position tracking, exits."""
from __future__ import annotations
import time
from datetime import datetime, timezone
import pandas as pd
from .config import AppCfg
from .presets import get_preset
from . import strategy as S
from .sizing import plan_qty
from .risk import RiskState
from .state import connect, audit, save_open, load_open, clear_open
from . import exchanges as X
from . import telegram as TG
from .logging_setup import get_logger

log = get_logger("engine")

TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60}
DEFAULT_TP_WEIGHTS = [30.0, 25.0, 15.0, 10.0]

def settle_currency(symbol: str) -> str:
    return symbol.split(":")[1] if ":" in symbol else symbol.split("/")[1]

def get_equity(ex, symbol: str, mode: str) -> float:
    if mode != "live":
        return 10000.0
    bal = ex.fetch_balance().get("total", {}) or {}
    for c in [settle_currency(symbol), "USDT", "USDC", "USD"]:
        try:
            v = float(bal.get(c) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    raise RuntimeError(f"no usable balance for {symbol} (checked settle + USDT/USDC/USD)")

def round_price(ex, symbol: str, price: float) -> float:
    try:
        return float(ex.price_to_precision(symbol, price))
    except Exception:
        return price

def round_amount(ex, symbol: str, qty: float) -> float:
    try:
        return float(ex.amount_to_precision(symbol, qty))
    except Exception:
        return qty

NATIVE_BOOK_ONLY = ("TP", "SL", "BE", "TRAIL")


def send_market_close(order_mode: str, reason: str, live: bool) -> bool:
    """In exchange_native, TP/SL/BE/TRAIL execute on the exchange; only OPP/OSC need a market close."""
    return not (live and order_mode == "exchange_native" and reason in NATIVE_BOOK_ONLY)


def meets_min_notional(ex, symbol: str, qty: float, price: float, live: bool) -> bool:
    """Blockt Entries unter Exchange-Minimum (Fail-safe: live ohne Limits = blockiert)."""
    try:
        lim = X.market_limits(ex, symbol)
    except Exception:
        return not live
    min_amt = lim.get("min_amount") or 0
    min_cost = lim.get("min_cost") or 0
    return not ((min_amt and qty < min_amt) or (min_cost and qty * price < min_cost))


def closed_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Signals only on confirmed candles (Pine timing=Kerzenschluss, no repaint)."""
    return df.iloc[:-1] if len(df) > 1 else df


def fetch_frame(ex, symbol: str, tf: str, limit: int = 600) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def last_price(ex, symbol: str, fallback: float) -> float:
    try:
        t = ex.fetch_ticker(symbol)
        px = t.get("last") or t.get("close")
        return float(px) if px else fallback
    except Exception:
        return fallback

_EXTRA_KEYS = ("pivot_len", "osc_low", "osc_high", "osc_rsi_len", "osc_stoch_len",
                "osc_k_len", "osc_d_len", "tp_weights", "fee_pct", "be_r", "trail_r",
                "osc_mode", "osc_entries", "osc_exit_after", "entry_mode", "legs",
                "session_on", "sizing", "risk_pct", "capital_pct")

def evaluate(df: pd.DataFrame, preset: dict, manual: dict) -> dict:
    manual = manual or {}
    p = {**preset, **{k: v for k, v in manual.items() if v is not None and (k in preset or k in _EXTRA_KEYS)}}
    f = S.trend_signals(S.compute_frame(df, p), p)
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
    base_ok = bool(pd.notna(last["atr"]) and pd.notna(last["trend_line"]))
    return {"long": bool(last["turn_long"]) and filt and vol_ok and base_ok,
            "short": bool(last["turn_short"]) and filt and vol_ok and base_ok,
            "close": float(last["close"]), "high": float(last["high"]), "low": float(last["low"]),
            "atr": float(last["atr"] if pd.notna(last["atr"]) else 0.0),
            "trend_line": float(last["trend_line"] if pd.notna(last["trend_line"]) else last["close"]),
            "osc_long": bool(last.get("osc_cross_up", False)),
            "osc_short": bool(last.get("osc_cross_dn", False))}

def entry_prices(anchor: float, side: str, stop_mode: str, preset: dict, atr_v: float) -> float:
    if stop_mode == "Prozent":
        return anchor * (1 - (1 if side == "long" else -1) * float(preset.get("stop_pct", 3.0)) / 100)
    return anchor - (1 if side == "long" else -1) * atr_v * float(preset.get("stop_atr_mult", 6.0))

def build_cycle(cycle_no: int, side: str, qty: float, entry: float, stop: float,
                preset: dict, manual: dict, atr_v: float) -> dict:
    tp_count = int(preset.get("tp_count", 4))
    dists = S.tp_distances(entry, stop, atr_v, preset, tp_count)
    weights = list((manual or {}).get("tp_weights", DEFAULT_TP_WEIGHTS))
    tps = []
    # split by weight proportionally on nominal qty (exchange rounding at order time)
    w = weights[:tp_count]
    s = sum(w)
    acc = 0.0
    for j in range(tp_count):
        if j < tp_count - 1:
            q = qty * w[j] / s
            acc += q
        else:
            q = qty - acc
        px = entry + (1 if side == "long" else -1) * dists[j]
        tps.append({"price": px, "qty": q, "hit": False})
    return {"cycle": cycle_no, "side": side, "qty": qty, "entry": entry, "stop": stop,
            "stop_reason": "SL", "tps": tps, "extreme": entry, "realized": 0.0,
            "risk_r": abs(entry - stop),
            "be_mode": preset.get("be_mode", "Aus"), "be_buffer": float(preset.get("be_buffer", 0.0)),
            "trail_mode": preset.get("trail_mode", "Aus"), "trail_atr": float(preset.get("trail_atr", 3.0)),
            "trail_pct": float(preset.get("trail_pct", 1.0)),
            "opposite_mode": preset.get("opposite_mode", "Ignorieren"),
            "fee_pct": float((manual or {}).get("fee_pct", 0.05)),
            "opened_at": datetime.now(timezone.utc).isoformat()}

def tranche_pnl(side: str, entry: float, px: float, qty: float, fee_pct: float) -> float:
    gross = (1 if side == "long" else -1) * (px - entry) * qty
    return gross - abs(px * qty) * fee_pct / 100.0 - abs(entry * qty) * fee_pct / 100.0

def monitor(cycle: dict, price: float, hi: float, lo: float, atr_v: float,
            trend_line: float, sig: dict, manual: dict) -> dict:
    """Advance one open cycle. Returns events: closes=[(qty,px,reason)], new_stop, hits, closed_all."""
    side, entry = cycle["side"], cycle["entry"]
    sgn = 1 if side == "long" else -1
    fav = max(hi, price) if side == "long" else min(lo, price)
    unf = min(lo, price) if side == "long" else max(hi, price)
    cycle["extreme"] = max(cycle["extreme"], fav) if side == "long" else min(cycle["extreme"], fav)
    rr = sgn * (cycle["extreme"] - entry) / cycle["risk_r"] if cycle["risk_r"] > 0 else 0.0
    out = {"closes": [], "new_stop": None, "closed_all": False, "why": ""}
    # 1. stop hit?
    if (side == "long" and unf <= cycle["stop"]) or (side == "short" and unf >= cycle["stop"]):
        rem = sum(t["qty"] for t in cycle["tps"] if not t["hit"])
        if rem > 0:
            out["closes"].append((rem, cycle["stop"], cycle["stop_reason"]))
        out["closed_all"] = True
        out["why"] = cycle["stop_reason"]
        return out
    # 2. TP hits in order
    for t in cycle["tps"]:
        if t["hit"]:
            continue
        if (side == "long" and fav >= t["price"]) or (side == "short" and fav <= t["price"]):
            t["hit"] = True
            out["closes"].append((t["qty"], t["price"], "TP"))
    # 3. break-even (fresh hits: TPs filled above already count)
    cur_hits = [t["hit"] for t in cycle["tps"]]
    be_mode = cycle["be_mode"]
    be_thr = float((manual or {}).get("be_r", 1.0))
    if be_mode != "Aus" and S.trigger_hit(be_mode, cur_hits, rr, be_thr):
        buf = entry * cycle["be_buffer"] / 100.0
        cand = entry + sgn * buf
        if sgn * (cand - cycle["stop"]) > 0:
            cycle["stop"], cycle["stop_reason"] = cand, "BE"
            out["new_stop"] = cand
    # 4. trailing
    tm = cycle["trail_mode"]
    tr_thr = float((manual or {}).get("trail_r", 1.0))
    if tm != "Aus" and S.trigger_hit("TP1", cur_hits, rr, tr_thr):
        cand = None
        if tm == "ATR":
            cand = cycle["extreme"] - sgn * atr_v * cycle["trail_atr"]
        elif tm == "Prozent":
            cand = cycle["extreme"] * (1 - sgn * cycle["trail_pct"] / 100.0)
        elif tm == "Trendlinie":
            cand = trend_line
        if cand is not None and sgn * (cand - cycle["stop"]) > 0:
            cycle["stop"], cycle["stop_reason"] = cand, "TRAIL"
            out["new_stop"] = cand
    # 5. opposite signal
    opp = (side == "long" and sig["short"]) or (side == "short" and sig["long"])
    if opp:
        if cycle["opposite_mode"] == "Schliessen":
            rem = sum(t["qty"] for t in cycle["tps"] if not t["hit"])
            if rem > 0:
                out["closes"].append((rem, price, "OPP"))
            out["closed_all"] = True
            out["why"] = "OPP"
        elif cycle["opposite_mode"] == "Schliessen und drehen":
            rem = sum(t["qty"] for t in cycle["tps"] if not t["hit"])
            if rem > 0:
                out["closes"].append((rem, price, "OPP"))
            out["closed_all"] = True
            out["why"] = "OPP_REVERSE"
    # 6. oscillator exit (only when manually enabled)
    if (manual or {}).get("osc_mode", "Aus") != "Aus":
        ox = (side == "long" and sig.get("osc_short")) or (side == "short" and sig.get("osc_long"))
        gate = (manual or {}).get("osc_exit_after", "Nach TP1")
        ok = gate == "Immer" or (gate == "Im Gewinn" and rr > 0) or (gate == "Nach TP1" and any(t["hit"] for t in cycle["tps"]))
        if ox and ok and not out["closed_all"]:
            rem = sum(t["qty"] for t in cycle["tps"] if not t["hit"])
            if rem > 0:
                out["closes"].append((rem, price, "OSC"))
            out["closed_all"] = True
            out["why"] = "OSC"
    return out

def run_loop(cfg: AppCfg, once: bool = False) -> None:
    risk = RiskState(killed=cfg.risk.kill_switch_on_start, kill_reason="kill_switch_on_start" if cfg.risk.kill_switch_on_start else "")
    db = connect(cfg.state_path)
    if (cfg.manual or {}).get("legs", 1) != 1 or (cfg.manual or {}).get("entry_mode", "Market") != "Market":
        raise SystemExit("nur legs=1 + entry_mode=Market werden unterstuetzt (Fail-fast statt stiller Fehlberechnung)")
    ex_map: dict = {}
    lev_ok: dict[str, bool] = {}
    warned: set[str] = set()
    for s in cfg.symbols:
        if s.enabled and s.exchange not in ex_map:
            try:
                ex_map[s.exchange] = X.build(s.exchange, need_auth=(cfg.mode == "live"))
            except Exception as e:
                log.error("exchange init failed", extra={"exchange": s.exchange, "error": str(e)})
                raise
    # leverage push (live): fail-safe per symbol
    for s in cfg.symbols:
        if not s.enabled or cfg.mode != "live":
            continue
        ok = X.set_leverage(ex_map[s.exchange], s.symbol, s.leverage)
        lev_ok[s.symbol] = ok
        if not ok:
            log.error("leverage NOT applied, symbol blocked", extra={"symbol": s.symbol, "leverage": s.leverage})
        audit(db, "engine", "leverage", f"{s.exchange} {s.symbol} x{s.leverage} ok={ok}")
    audit(db, "engine", "start", f"mode={cfg.mode} symbols={len(cfg.symbols)}")
    if cfg.mode == "live":
        TG.send(f"Bot gestartet (LIVE): {len(cfg.symbols)} Symbole")
    cycle_no = 0
    tf_min = {s.symbol: TF_MINUTES.get(s.timeframe, 60) for s in cfg.symbols}
    while True:
        for s in cfg.symbols:
            if not s.enabled:
                continue
            ex = ex_map[s.exchange]
            key = f"{s.exchange}:{s.symbol}"
            try:
                preset = get_preset(s.asset_preset, s.timeframe, cfg.fine_tune)
                df = fetch_frame(ex, s.symbol, s.timeframe)
                if len(df) < 2:
                    continue
                sig = evaluate(closed_frame(df), preset, cfg.manual or {})
                price = last_price(ex, s.symbol, sig["close"]) if cfg.mode == "live" else sig["close"]
                forming = df.iloc[-1]
                ex_hi = max(float(forming["high"]), price)
                ex_lo = min(float(forming["low"]), price)
                cycle = load_open(db, s.exchange, s.symbol)
                # reconcile live exchange state
                live_qty = X.fetch_position_qty(ex, s.symbol) if cfg.mode == "live" else 0.0
                if cycle and cfg.mode == "live" and live_qty == 0.0 and not cycle.get("just_opened"):
                    # externally closed: finalize with estimate
                    pnl = tranche_pnl(cycle["side"], cycle["entry"], price,
                                      sum(t["qty"] for t in cycle["tps"] if not t["hit"]), cycle["fee_pct"]) + cycle["realized"]
                    try:
                        eqx = get_equity(ex, s.symbol, cfg.mode)
                    except Exception:
                        eqx = 10000.0
                    finish_close(db, risk, cfg, s, cycle, pnl, price, "EXTERN", eqx)
                    cycle = None
                elif not cycle and cfg.mode == "live" and live_qty != 0.0 and key not in warned:
                    warned.add(key)
                    msg = f"Unverwaltete Position {s.exchange} {s.symbol} ({live_qty}) - Entries blockiert bis flat"
                    log.error("unmanaged position", extra={"symbol": s.symbol})
                    if cfg.telegram.enabled:
                        TG.send(msg)
                if cycle:
                    if cycle.pop("just_opened", False):
                        save_open(db, s.exchange, s.symbol, cycle)
                        continue
                    # refresh entry from broker once (avg fill)
                    if cfg.mode == "live" and not cycle.get("entry_synced"):
                        try:
                            for p in ex.fetch_positions([s.symbol]):
                                ep = float(p.get("entryPrice") or 0)
                                if ep > 0:
                                    cycle["entry"] = ep
                                    break
                            cycle["entry_synced"] = True
                        except Exception:
                            pass
                    ev = monitor(cycle, price, ex_hi, ex_lo, sig["atr"], sig["trend_line"], sig, cfg.manual or {})
                    for qty, px, reason in ev["closes"]:
                        if not send_market_close(s.order_mode, reason, cfg.mode == "live"):
                            cycle["realized"] += tranche_pnl(cycle["side"], cycle["entry"], px, qty, cycle["fee_pct"])
                            continue  # exchange executes TP/SL natively; only book it
                        rq = round_amount(ex, s.symbol, qty) if cfg.mode == "live" else qty
                        if cfg.mode == "live":
                            from .orders import close_position, client_id
                            try:
                                close_position(ex, s.symbol, cycle["side"], rq, client_id(cycle["cycle"], 0, 7))
                            except Exception as e:
                                log.error("close failed", extra={"symbol": s.symbol, "error": str(e)})
                                continue
                        cycle["realized"] += tranche_pnl(cycle["side"], cycle["entry"], px, qty, cycle["fee_pct"])
                    if s.order_mode == "exchange_native" and ev["new_stop"] and cfg.mode == "live":
                        move_native_stop(ex, s.symbol, cycle)
                    if ev["closed_all"]:
                        try:
                            eq = get_equity(ex, s.symbol, cfg.mode)
                        except Exception:
                            eq = 10000.0
                        finish_close(db, risk, cfg, s, cycle, cycle["realized"], price, ev["why"], eq)
                        cycle = None
                        if ev["why"] != "OPP_REVERSE":
                            continue  # normal close: fallthrough only for reverse entries
                    else:
                        save_open(db, s.exchange, s.symbol, cycle)
                        continue
                # --- flat: entry check ---
                if risk.killed:
                    continue
                if cfg.mode == "live" and lev_ok.get(s.symbol) is False:
                    continue  # leverage not applied -> fail-safe block
                if cfg.mode == "live" and live_qty != 0.0:
                    continue  # unmanaged or residual position
                if not cooldown_ok(risk, key, tf_min[s.symbol], cfg.risk.cooldown_bars):
                    continue
                want = "long" if sig["long"] else ("short" if sig["short"] else "")
                if not want:
                    continue
                equity = get_equity(ex, s.symbol, cfg.mode)
                anchor = price
                stop = round_price(ex, s.symbol, entry_prices(anchor, want, preset.get("stop_mode", "Prozent"), preset, sig["atr"])) if cfg.mode == "live" else entry_prices(anchor, want, preset.get("stop_mode", "Prozent"), preset, sig["atr"])
                if want == "long" and anchor <= stop or want == "short" and anchor >= stop:
                    continue
                qtys, _, _, err = plan_qty(equity, anchor, 1.0,
                                           (cfg.manual or {}).get("sizing", "Stoprisiko"),
                                           float((cfg.manual or {}).get("risk_pct", 1.0)),
                                           float((cfg.manual or {}).get("capital_pct", 10.0)),
                                           s.leverage, cfg.risk.max_position_value_pct,
                                           abs(anchor - stop), [1.0], 1, int(preset.get("tp_count", 4)), 0.001)
                if err or not qtys:
                    log.error("sizing blocked", extra={"symbol": s.symbol, "error": err})
                    continue
                qty = round_amount(ex, s.symbol, qtys[0]) if cfg.mode == "live" else qtys[0]
                if qty <= 0:
                    continue
                if not meets_min_notional(ex, s.symbol, qty, anchor, cfg.mode == "live"):
                    log.error("under exchange minimum, skipped", extra={"symbol": s.symbol, "qty": qty})
                    continue
                cycle_no += 1
                cycle = build_cycle(cycle_no, want, qty, anchor, stop, preset, cfg.manual or {}, sig["atr"])
                if cfg.mode == "live":
                    from .orders import place_entry, place_bracket, client_id
                    fill = place_entry(ex, s.symbol, want, qty, None, client_id(cycle_no, 0, 9))
                    try:
                        cycle["entry"] = float(fill.get("average") or fill.get("price") or anchor)
                    except Exception:
                        cycle["entry"] = anchor
                    if s.order_mode == "exchange_native":
                        tps = [(round_price(ex, s.symbol, t["price"]), round_amount(ex, s.symbol, t["qty"])) for t in cycle["tps"]]
                        try:
                            placed = place_bracket(ex, s.symbol, want, qty, stop, tps, cycle_no)
                            if placed and placed[0].get("id"):
                                cycle["stop_order_id"] = placed[0]["id"]
                        except Exception as e:
                            log.error("bracket failed, closing entry", extra={"symbol": s.symbol, "error": str(e)})
                            from .orders import close_position
                            try:
                                close_position(ex, s.symbol, want, qty)
                            except Exception:
                                pass
                            continue
                cycle["just_opened"] = True
                save_open(db, s.exchange, s.symbol, cycle)
                audit(db, "engine", "entry", f"{s.exchange} {s.symbol} {want} qty={qty} entry={anchor} stop={stop}")
                if cfg.telegram.enabled and (cfg.telegram.notify_open if hasattr(cfg.telegram, "notify_open") else True):
                    TG.trade_open(s.exchange, s.symbol, want, qty, anchor, stop)
            except SystemExit:
                raise
            except Exception as e:
                log.error("symbol loop error", extra={"symbol": s.symbol, "error": str(e)})
                if cfg.telegram.enabled and cfg.telegram.notify_errors:
                    TG.send(f"FEHLER {s.exchange} {s.symbol}: {e}")
        # auto kill-switch: close everything
        if risk.killed and not getattr(run_loop, "_kill_closed", False):
            run_loop._kill_closed = True  # type: ignore[attr-defined]
            for s in cfg.symbols:
                if s.enabled and cfg.mode == "live":
                    from .orders import close_all
                    try:
                        close_all(ex_map[s.exchange], s.symbol)
                    except Exception:
                        pass
            if cfg.telegram.enabled and cfg.telegram.notify_kill:
                TG.killed(risk.kill_reason)
        if once:
            break
        time.sleep(max(5, cfg.poll_seconds))

def cooldown_ok(risk: RiskState, key: str, tf_minutes: int, cooldown_bars: int) -> bool:
    import time as _t
    last = risk.last_flat_bar.get(key)
    if last is None:
        return True
    return _t.time() >= last + cooldown_bars * tf_minutes * 60

def finish_close(db, risk: RiskState, cfg: AppCfg, s, cycle: dict, pnl: float, price: float, why: str, equity: float = 10000.0) -> None:
    from datetime import datetime, timezone as _tz
    import time as _t
    db.execute("INSERT INTO cycles(ts,exchange,symbol,side,entry,stop,qty,pnl,note) VALUES (?,?,?,?,?,?,?,?,?)",
               (datetime.now(_tz.utc).isoformat(), s.exchange, s.symbol,
                1 if cycle["side"] == "long" else -1, cycle["entry"], cycle["stop"], cycle["qty"], pnl, why))
    db.commit()
    clear_open(db, s.exchange, s.symbol)
    risk.last_flat_bar[f"{s.exchange}:{s.symbol}"] = _t.time()
    try:
        msg = risk.register_pnl(pnl, cfg.risk.max_daily_loss_pct, equity)
        if msg:
            log.error("kill switch tripped", extra={"reason": msg})
    except Exception as e:
        log.error("risk register failed", extra={"error": str(e)})
    audit(db, "engine", "close", f"{s.exchange} {s.symbol} {cycle['side']} pnl={pnl:.2f} why={why} @ {price}")
    if cfg.telegram.enabled and cfg.telegram.notify_close:
        TG.trade_close(s.exchange, s.symbol, pnl, why)

def move_native_stop(ex, symbol: str, cycle: dict) -> None:
    """Move exchange-native SL after BE/trailing: cancel tracked stop, place new one."""
    from .orders import stop_market_type, client_id
    try:
        side = cycle["side"]
        exit_side = "sell" if side == "long" else "buy"
        cancelled = False
        if cycle.get("stop_order_id"):
            try:
                ex.cancel_order(cycle["stop_order_id"], symbol)
                cancelled = True
            except Exception:
                pass
        if not cancelled:
            for o in ex.fetch_open_orders(symbol):
                blob = str(o.get("type", "")) + str(o.get("info", ""))
                if "stop" in blob.lower() or "trigger" in blob.lower():
                    try:
                        ex.cancel_order(o["id"], symbol)
                    except Exception:
                        pass
        live = abs(X.fetch_position_qty(ex, symbol))
        remaining = live if live > 0 else sum(t["qty"] for t in cycle["tps"] if not t["hit"])
        if remaining > 0:
            oid = ex.create_order(symbol, stop_market_type(ex), exit_side, remaining, None,
                                {"stopPrice": cycle["stop"], "reduceOnly": True,
                                 "clientOrderId": client_id(cycle["cycle"], 0, 8)})
            if isinstance(oid, dict) and oid.get("id"):
                cycle["stop_order_id"] = oid["id"]
    except Exception as e:
        log.error("move native stop failed", extra={"symbol": symbol, "error": str(e)})
