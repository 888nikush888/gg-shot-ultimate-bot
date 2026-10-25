"""Main loop: one process, N symbols, paper/live, leverage, position tracking, exits."""

from __future__ import annotations
import time
from contextlib import suppress
from datetime import datetime, timezone
import pandas as pd
from .config import AppCfg
from .presets import get_preset
from . import strategy as S
from .sizing import plan_qty
from .risk import RiskState
from .state import connect, audit, save_open, load_open, clear_open, kill_flag_reason
from . import exchanges as X
from . import telegram as TG
from .logging_setup import get_logger

log = get_logger("engine")

TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60}
DEFAULT_TP_WEIGHTS = [30.0, 25.0, 15.0, 10.0]


def settle_currency(symbol: str) -> str:
    head, _, tail = symbol.partition(":")
    if tail:
        return tail
    parts = head.split("/")
    return parts[1] if len(parts) > 1 else ""


def get_equity(ex, symbol: str, mode: str) -> float:
    if mode != "live":
        return 10000.0
    bal = ex.fetch_balance().get("total", {}) or {}
    for c in [c for c in [settle_currency(symbol), "USDT", "USDC", "USD"] if c]:
        try:
            v = float(bal.get(c) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    raise RuntimeError(
        f"no usable balance for {symbol} (checked settle + USDT/USDC/USD)"
    )


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


_EQ_CACHE: dict[str, tuple[float, float]] = {}


def should_send_error(
    cache: dict[str, float], key: str, now: float, ttl: float = 900.0
) -> bool:
    """Max. 1 Fehlermeldung pro Symbol und Viertelstunde (kein Telegram-Spam)."""
    last = cache.get(key)
    if last is not None and now - last < ttl:
        return False
    cache[key] = now
    return True


def cached_equity(key: str, ex, symbol: str, mode: str, ttl: float = 60.0) -> float:

    now = time.time()
    hit = _EQ_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    v = get_equity(ex, symbol, mode)
    _EQ_CACHE[key] = (now, v)
    return v


def save_open_if_changed(
    db, exchange: str, symbol: str, payload: dict, cache: dict[str, str]
) -> bool:
    import hashlib as _h
    import json as _j

    h = _h.sha256(_j.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    k = exchange + ":" + symbol
    if cache.get(k) == h:
        return False
    save_open(db, exchange, symbol, payload)
    cache[k] = h
    return True


def should_resize_native_stop(order_mode: str, live: bool, closes: list) -> bool:
    """Re-size nativen SL nach jedem nativen TP-Fill (Restmenge schrumpft)."""
    return (
        live
        and order_mode == "exchange_native"
        and any(r == "TP" for _, _, r in closes)
    )


KNOWN_MANUAL_KEYS = frozenset(
    [
        "mode",
        "timing",
        "allow_long",
        "allow_short",
        "sizing",
        "risk_pct",
        "capital_pct",
        "legs",
        "entry_mode",
        "entry_unit",
        "pullback_distance",
        "limit_life",
        "tp_weights",
        "fee_pct",
        "be_r",
        "trail_r",
        "trail_start",
        "tp_mode_1",
        "tp_mode_2",
        "tp_mode_3",
        "tp_mode_4",
        "bounce_on",
        "sr_bounce_on",
        "reentry_on",
        "max_reentries",
        "zone_atr",
        "osc_mode",
        "osc_entries",
        "osc_reentries",
        "osc_exit",
        "osc_exit_after",
        "osc_rsi_len",
        "osc_stoch_len",
        "osc_k_len",
        "osc_d_len",
        "osc_low",
        "osc_high",
        "div_mode",
        "div_age",
        "pivot_len",
        "session_on",
        "trade_session",
        "session_days",
        "session_zone",
        "date_start",
        "date_end",
        "close_session",
        "volume_len",
        "tp_adapt_min",
        "tp_adapt_max",
        "profit_trail_1",
        "profit_trail_2",
        "profit_trail_3",
        "profit_trail_4",
        "profit_trail_unit",
        "stop_pad_ticks",
        "stop_pct",
        "stop_atr_mult",
        "atr_len",
        "regime_len",
        "adapt_min",
        "adapt_max",
    ]
)


def unknown_manual_keys(
    manual: dict, known: frozenset = KNOWN_MANUAL_KEYS
) -> list[str]:
    from . import presets as _pp

    base: set[str] = set(known)
    with suppress(Exception):
        db = _pp.load_presets()
        for _k, _v in db.items():
            if not _k.startswith("_") and isinstance(_v, dict):
                base.update(_v.keys())
    return sorted(set((manual or {}).keys()) - base)


def validate_manual(manual: dict | None) -> dict:
    from .strategy import normalize_manual

    man = normalize_manual(manual)
    if man.get("mode", "Standard") not in ("Standard", "Advanced", "Channel"):
        raise SystemExit(
            "mode muss Standard/Advanced/Channel sein (Classic braucht Gewinn-Trailing-Engine)"
        )
    for _j in range(1, 5):
        if man.get(f"tp_mode_{_j}", "Fest") != "Fest":
            raise SystemExit(
                "nur tp_mode Fest wird unterstuetzt (ATR-adaptiv/Gewinn-Trailing braucht Ziel-Neuberechnung)"
            )
    if man.get("legs", 1) != 1 or man.get("entry_mode", "Market") not in (
        "Market",
        "Pullback-Limit",
    ):
        raise SystemExit(
            "nur legs=1 + entry_mode Market/Pullback-Limit werden unterstuetzt (kein Gestaffelt)"
        )
    _w = man.get("tp_weights", [30.0, 25.0, 15.0, 10.0])
    if (
        not isinstance(_w, list)
        or len(_w) < 1
        or not all(isinstance(x, (int, float)) and x > 0 for x in _w)
    ):
        raise SystemExit("tp_weights braucht eine nicht-leere Liste positiver Zahlen")
    if man.get("trail_start", "TP1") not in ("Sofort", "TP1", "TP2", "TP3", "TP4", "R"):
        raise SystemExit("trail_start muss Sofort/TP1-4/R sein")
    return man


def pullback_anchor(price: float, side: str, atr_v: float, manual: dict) -> float:
    sgn = 1 if side == "long" else -1
    if manual.get("entry_unit", "ATR") == "ATR":
        return price - sgn * atr_v * float(manual.get("pullback_distance", 0.5))
    return price - sgn * price * float(manual.get("pullback_distance", 0.5)) / 100.0


def closed_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Signals only on confirmed candles (Pine timing=Kerzenschluss, no repaint)."""
    return df.iloc[:-1] if len(df) > 1 else df


def fetch_frame(ex, symbol: str, tf: str, limit: int = 600) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df.loc[:, "ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def last_price(ex, symbol: str, fallback: float) -> float:
    try:
        t = ex.fetch_ticker(symbol)
        px = t.get("last") or t.get("close")
        return float(px) if px else fallback
    except Exception:
        return fallback


def _evaluate_frame(df, preset, manual):
    manual = S.normalize_manual(manual)
    p = {**preset, **{k: v for k, v in manual.items() if v is not None}}
    f = S.trend_signals(S.compute_frame(df, p), p)
    return p, f.iloc[-1]


def _filter_state(p, last):
    atr_tr = bool(last["f_atr"] >= last["f_avg"]) if pd.notna(last["f_atr"]) else False
    rsi_tr = bool(
        last["rsi"] > float(p.get("rsi_upper", 45))
        or last["rsi"] < float(p.get("rsi_lower", 10))
    )
    flat = bool(
        last["rsi"] < float(p.get("rsi_upper", 45))
        and last["rsi"] > float(p.get("rsi_lower", 10))
    )
    filt = {
        "Aus": True,
        "ATR": atr_tr,
        "RSI": rsi_tr,
        "ATR oder RSI": atr_tr or rsi_tr,
        "ATR und RSI": atr_tr and rsi_tr,
        "Seitwaerts: ATR oder RSI": (not atr_tr) or flat,
        "Seitwaerts: ATR und RSI": (not atr_tr) and flat,
    }.get(p.get("filter_mode", "Aus"), True)
    vol_ok = True
    if p.get("volume_on"):
        va = last.get("vol_avg", float("nan"))
        vol_ok = bool(
            pd.notna(va)
            and va > 0
            and last["volume"] / va >= float(p.get("volume_min", 1.0))
        )
    base_ok = bool(pd.notna(last["atr"]) and pd.notna(last["trend_line"]))
    return filt, vol_ok, base_ok


def _osc_state(p, last):
    osc_mode = p.get("osc_mode", "Aus")
    tdir = int(last["trend_dir"]) if pd.notna(last["trend_dir"]) else 0
    osc_l = (
        osc_mode != "Aus"
        and bool(last.get("osc_cross_up", False))
        and (osc_mode == "Beide Richtungen" or tdir == 1)
    )
    osc_s = (
        osc_mode != "Aus"
        and bool(last.get("osc_cross_dn", False))
        and (osc_mode == "Beide Richtungen" or tdir == -1)
    )
    return osc_l, osc_s


def _reentry_ok(p, trend_entries) -> bool:
    return (
        p.get("mode", "Standard") == "Advanced"
        and bool(p.get("reentry_on", False))
        and trend_entries <= int(p.get("max_reentries", 1))
    )


def _extra_entries(p, last, osc_l, osc_s, trend_entries):
    re_ok = _reentry_ok(p, trend_entries)
    extra_l = bool(last.get("bounce_long", False)) or (
        bool(p.get("osc_reentries", False)) and re_ok and trend_entries > 0 and osc_l
    )
    extra_s = bool(last.get("bounce_short", False)) or (
        bool(p.get("osc_reentries", False)) and re_ok and trend_entries > 0 and osc_s
    )
    if trend_entries > 0:
        extra_l = extra_l and re_ok
        extra_s = extra_s and re_ok
    return extra_l, extra_s


def _apply_entry_gates(p, last, want_l, want_s):
    if p.get("div_mode", "Aus") == "Filter":
        div_age = float(p.get("div_age", 20))
        want_l = want_l and bool(last.get("bull_div_age", float("inf")) <= div_age)
        want_s = want_s and bool(last.get("bear_div_age", float("inf")) <= div_age)
    if not p.get("allow_long", True):
        want_l = False
    if not p.get("allow_short", True):
        want_s = False
    return want_l, want_s


def _want_state(p, last, osc_l, osc_s, trend_entries):
    extra_l, extra_s = _extra_entries(p, last, osc_l, osc_s, trend_entries)
    want_l = bool(last["turn_long"]) or extra_l
    want_s = bool(last["turn_short"]) or extra_s
    if not want_l and not want_s and bool(p.get("osc_entries", False)):
        want_l, want_s = osc_l, osc_s
    return _apply_entry_gates(p, last, want_l, want_s)


def _signal_levels(last):
    return {
        "support": float(last["support"])
        if pd.notna(last.get("support", float("nan")))
        else None,
        "resistance": float(last["resistance"])
        if pd.notna(last.get("resistance", float("nan")))
        else None,
        "bb_up": float(last["bb_up"])
        if pd.notna(last.get("bb_up", float("nan")))
        else None,
        "bb_dn": float(last["bb_dn"])
        if pd.notna(last.get("bb_dn", float("nan")))
        else None,
        "rng_up": float(last["upper"])
        if pd.notna(last.get("upper", float("nan")))
        else None,
        "rng_lo": float(last["lower"])
        if pd.notna(last.get("lower", float("nan")))
        else None,
    }


def evaluate(
    df: pd.DataFrame, preset: dict, manual: dict, trend_entries: int = 0
) -> dict:
    p, last = _evaluate_frame(df, preset, manual)
    filt, vol_ok, base_ok = _filter_state(p, last)
    osc_l, osc_s = _osc_state(p, last)
    want_l, want_s = _want_state(p, last, osc_l, osc_s, trend_entries)
    now = datetime.now(timezone.utc)
    sess_ok = S.session_allows(now, p) and S.date_allows(now, p)
    return {
        "long": want_l and filt and vol_ok and base_ok,
        "short": want_s and filt and vol_ok and base_ok,
        "session_ok": sess_ok,
        "close": float(last["close"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "atr": float(last["atr"] if pd.notna(last["atr"]) else 0.0),
        "trend_line": float(
            last["trend_line"] if pd.notna(last["trend_line"]) else last["close"]
        ),
        "trend_no": int(last.get("trend_no", 0)),
        **_signal_levels(last),
        "osc_long": osc_l,
        "osc_short": osc_s,
        "bounce_long": bool(last.get("bounce_long", False)),
        "bounce_short": bool(last.get("bounce_short", False)),
    }


def _ref_pad(ref):
    tick = ref.get("tick")
    return float(ref.get("pad_ticks", 1)) * tick if tick else None


def _stop_prozent(anchor, sgn, preset, _atr_v):
    return anchor * (1 - sgn * float(preset.get("stop_pct", 3.0)) / 100)


def _stop_atr(anchor, sgn, preset, atr_v):
    return anchor - sgn * atr_v * float(preset.get("stop_atr_mult", 6.0))


def _stop_trendlinie(_anchor, sgn, ref):
    pad = _ref_pad(ref)
    if ref.get("trend_line") is None or pad is None:
        raise ValueError("Trendlinien-Stop braucht trend_line + tick")
    return float(ref["trend_line"]) - sgn * pad


def _stop_sr(_anchor, sgn, side, ref):
    lvl = ref.get("support") if side == "long" else ref.get("resistance")
    pad = _ref_pad(ref)
    if lvl is None or pad is None:
        raise ValueError("S/R-Stop braucht Support/Resistance + tick")
    return float(lvl) - sgn * pad


def _stop_auto(anchor, sgn, _preset, _atr_v, ref):
    rev = anchor - sgn * abs(anchor - float(ref.get("reverse", anchor)))
    pad = _ref_pad(ref)
    if pad is not None:
        rev = rev - sgn * pad
    return rev


def entry_prices(
    anchor: float,
    side: str,
    stop_mode: str,
    preset: dict,
    atr_v: float,
    ref: dict | None = None,
) -> float:
    sgn = 1 if side == "long" else -1
    if stop_mode == "Prozent":
        return _stop_prozent(anchor, sgn, preset, atr_v)
    if stop_mode == "ATR":
        return _stop_atr(anchor, sgn, preset, atr_v)
    if ref is None:
        raise ValueError(
            f"stop_mode {stop_mode} braucht Trendlinie/S-R-Referenz (kein stiller ATR-Fallback)"
        )
    if stop_mode == "Trendlinie":
        return _stop_trendlinie(anchor, sgn, ref)
    if stop_mode == "S/R":
        return _stop_sr(anchor, sgn, side, ref)
    if stop_mode == "Automatisch":
        pct = _stop_prozent(anchor, sgn, preset, atr_v)
        rev = _stop_auto(anchor, sgn, preset, atr_v, ref)
        return max(pct, rev) if side == "long" else min(pct, rev)
    raise ValueError(f"unbekannter stop_mode: {stop_mode}")


def build_cycle(
    cycle_no: int,
    side: str,
    qty: float,
    entry: float,
    stop: float,
    preset: dict,
    manual: dict,
    atr_v: float,
) -> dict:
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
    return {
        "cycle": cycle_no,
        "side": side,
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "stop_reason": "SL",
        "tps": tps,
        "extreme": entry,
        "realized": 0.0,
        "risk_r": abs(entry - stop),
        "be_mode": preset.get("be_mode", "Aus"),
        "be_buffer": float(preset.get("be_buffer", 0.0)),
        "trail_mode": preset.get("trail_mode", "Aus"),
        "trail_start": (manual or {}).get("trail_start", "TP1"),
        "trail_atr": float(preset.get("trail_atr", 3.0)),
        "trail_pct": float(preset.get("trail_pct", 1.0)),
        "opposite_mode": preset.get("opposite_mode", "Ignorieren"),
        "fee_pct": float((manual or {}).get("fee_pct", 0.05)),
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }


def tranche_pnl(
    side: str, entry: float, px: float, qty: float, fee_pct: float
) -> float:
    gross = (1 if side == "long" else -1) * (px - entry) * qty
    return gross - abs(px * qty) * fee_pct / 100.0 - abs(entry * qty) * fee_pct / 100.0


def _stop_fill(side, unf, cycle):
    if (side == "long" and unf <= cycle["stop"]) or (
        side == "short" and unf >= cycle["stop"]
    ):
        rem = sum(t["qty"] for t in cycle["tps"] if not t["hit"])
        if rem > 0:
            return [(rem, cycle["stop"], cycle["stop_reason"])], cycle["stop_reason"]
        return [], cycle["stop_reason"]
    return None


def _tp_fills(side, fav, cycle):
    out = []
    for t in cycle["tps"]:
        if t["hit"]:
            continue
        if (side == "long" and fav >= t["price"]) or (
            side == "short" and fav <= t["price"]
        ):
            t["hit"] = True
            out.append((t["qty"], t["price"], "TP"))
    return out


def _maybe_break_even(cycle, rr, manual):
    cur_hits = [t["hit"] for t in cycle["tps"]]
    be_mode = cycle["be_mode"]
    be_thr = float((manual or {}).get("be_r", 1.0))
    if be_mode != "Aus" and S.trigger_hit(be_mode, cur_hits, rr, be_thr):
        sgn = 1 if cycle["side"] == "long" else -1
        buf = cycle["entry"] * cycle["be_buffer"] / 100.0
        cand = cycle["entry"] + sgn * buf
        if sgn * (cand - cycle["stop"]) > 0:
            cycle["stop"], cycle["stop_reason"] = cand, "BE"
            return cand
    return None


def _maybe_trail(cycle, rr, manual, atr_v, trend_line):
    tm = cycle["trail_mode"]
    tr_thr = float((manual or {}).get("trail_r", 1.0))
    cur_hits = [t["hit"] for t in cycle["tps"]]
    if tm != "Aus" and S.trigger_hit(
        cycle.get("trail_start", "TP1"), cur_hits, rr, tr_thr
    ):
        sgn = 1 if cycle["side"] == "long" else -1
        cand = None
        if tm == "ATR":
            cand = cycle["extreme"] - sgn * atr_v * cycle["trail_atr"]
        elif tm == "Prozent":
            cand = cycle["extreme"] * (1 - sgn * cycle["trail_pct"] / 100.0)
        elif tm == "Trendlinie":
            cand = trend_line
        if cand is not None and sgn * (cand - cycle["stop"]) > 0:
            cycle["stop"], cycle["stop_reason"] = cand, "TRAIL"
            return cand
    return None


def _opposite_close(side, sig, cycle, price):
    opp = (side == "long" and sig["short"]) or (side == "short" and sig["long"])
    if opp and cycle["opposite_mode"] in ("Schliessen", "Schliessen und drehen"):
        rem = sum(t["qty"] for t in cycle["tps"] if not t["hit"])
        out = [(rem, price, "OPP")] if rem > 0 else []
        if cycle["opposite_mode"] == "Schliessen und drehen":
            return out, "OPP_REVERSE"
        return out, "OPP"
    return None


def _osc_exit_ok(gate, rr, cycle):
    return (
        gate == "Immer"
        or (gate == "Im Gewinn" and rr > 0)
        or (gate == "Nach TP1" and any(t["hit"] for t in cycle["tps"]))
    )


def _osc_close(side, sig, cycle, price, manual, rr):
    if (manual or {}).get("osc_mode", "Aus") == "Aus":
        return None
    ox = (side == "long" and sig.get("osc_short")) or (
        side == "short" and sig.get("osc_long")
    )
    ok = _osc_exit_ok((manual or {}).get("osc_exit_after", "Nach TP1"), rr, cycle)
    if ox and ok:
        rem = sum(t["qty"] for t in cycle["tps"] if not t["hit"])
        out = [(rem, price, "OSC")] if rem > 0 else []
        return out, "OSC"
    return None


def monitor(
    cycle: dict,
    price: float,
    hi: float,
    lo: float,
    atr_v: float,
    trend_line: float,
    sig: dict,
    manual: dict,
) -> dict:
    # Advance one open cycle. Events: closes=[(qty,px,reason)], new_stop, hits, closed_all.
    side, entry = cycle["side"], cycle["entry"]
    sgn = 1 if side == "long" else -1
    fav = max(hi, price) if side == "long" else min(lo, price)
    unf = min(lo, price) if side == "long" else max(hi, price)
    cycle["extreme"] = (
        max(cycle["extreme"], fav) if side == "long" else min(cycle["extreme"], fav)
    )
    rr = (
        sgn * (cycle["extreme"] - entry) / cycle["risk_r"]
        if cycle["risk_r"] > 0
        else 0.0
    )
    out = {"closes": [], "new_stop": None, "closed_all": False, "why": ""}
    # 1. stop hit?
    stop_ev = _stop_fill(side, unf, cycle)
    if stop_ev is not None:
        out["closes"].extend(stop_ev[0])
        out["closed_all"] = True
        out["why"] = stop_ev[1]
        return out
    # 2. TP hits in order (fresh hits: TPs filled above already count)
    out["closes"].extend(_tp_fills(side, fav, cycle))
    # 3. break-even
    be = _maybe_break_even(cycle, rr, manual)
    if be is not None:
        out["new_stop"] = be
    # 4. trailing
    tr = _maybe_trail(cycle, rr, manual, atr_v, trend_line)
    if tr is not None:
        out["new_stop"] = tr
    # 5. opposite signal
    opp = _opposite_close(side, sig, cycle, price)
    if opp is not None:
        out["closes"].extend(opp[0])
        out["closed_all"] = True
        out["why"] = opp[1]
    # 6. oscillator exit (only when manually enabled)
    osc = _osc_close(side, sig, cycle, price, manual, rr)
    if osc is not None and not out["closed_all"]:
        out["closes"].extend(osc[0])
        out["closed_all"] = True
        out["why"] = osc[1]
    return out


def _check_manual_modes(man) -> dict:
    if man.get("mode", "Standard") not in ("Standard", "Advanced", "Channel"):
        raise SystemExit(
            "mode muss Standard/Advanced/Channel sein (Classic braucht Gewinn-Trailing-Engine)"
        )
    for _j in range(1, 5):
        if man.get(f"tp_mode_{_j}", "Fest") != "Fest":
            raise SystemExit(
                "nur tp_mode Fest wird unterstuetzt (ATR-adaptiv/Gewinn-Trailing braucht Ziel-Neuberechnung)"
            )
    return man


def _init_exchanges(cfg, db, ex_map, lev_ok) -> None:
    for s in cfg.symbols:
        if s.enabled and s.exchange not in ex_map:
            try:
                ex_map[s.exchange] = X.build(s.exchange, need_auth=(cfg.mode == "live"))
            except Exception as e:
                log.error(
                    "exchange init failed",
                    extra={"exchange": s.exchange, "error": str(e)},
                )
                raise
    # leverage push (live): fail-safe per symbol
    for s in cfg.symbols:
        if not s.enabled or cfg.mode != "live":
            continue
        ok = X.set_leverage(ex_map[s.exchange], s.symbol, s.leverage)
        lev_ok[s.symbol] = ok
        if not ok:
            log.error(
                "leverage NOT applied, symbol blocked",
                extra={"symbol": s.symbol, "leverage": s.leverage},
            )
        audit(
            db, "engine", "leverage", f"{s.exchange} {s.symbol} x{s.leverage} ok={ok}"
        )


def _warn_unknown_manual(cfg) -> None:
    _known = set()
    with suppress(Exception):
        from .presets import load_presets as _lp

        for _k, _v in _lp().items():
            if not _k.startswith("_") and isinstance(_v, dict):
                _known.update(_v.keys())
    _known.update(
        [
            "mode",
            "timing",
            "allow_long",
            "allow_short",
            "sizing",
            "risk_pct",
            "capital_pct",
            "legs",
            "entry_mode",
            "entry_unit",
            "pullback_distance",
            "limit_life",
            "tp_weights",
            "fee_pct",
            "be_r",
            "trail_r",
            "trail_start",
            "tp_mode_1",
            "tp_mode_2",
            "tp_mode_3",
            "tp_mode_4",
            "bounce_on",
            "sr_bounce_on",
            "reentry_on",
            "max_reentries",
            "zone_atr",
            "osc_mode",
            "osc_entries",
            "osc_reentries",
            "osc_exit",
            "osc_exit_after",
            "osc_rsi_len",
            "osc_stoch_len",
            "osc_k_len",
            "osc_d_len",
            "osc_low",
            "osc_high",
            "div_mode",
            "div_age",
            "pivot_len",
            "session_on",
            "trade_session",
            "session_days",
            "session_zone",
            "date_start",
            "date_end",
            "close_session",
            "volume_len",
            "tp_adapt_min",
            "tp_adapt_max",
            "profit_trail_1",
            "profit_trail_2",
            "profit_trail_3",
            "profit_trail_4",
            "profit_trail_unit",
            "stop_pad_ticks",
            "atr_len",
            "regime_len",
            "adapt_min",
            "adapt_max",
        ]
    )
    _unknown = sorted(set((cfg.manual or {}).keys()) - _known)
    if _unknown:
        log.error(
            "unbekannte manual-keys (Tippfehler?) werden ignoriert",
            extra={"keys": _unknown},
        )


def _setup_run(cfg: AppCfg) -> dict:
    _flag = kill_flag_reason(cfg.state_path)
    risk = RiskState(
        killed=cfg.risk.kill_switch_on_start or _flag is not None,
        kill_reason="kill_switch_on_start"
        if cfg.risk.kill_switch_on_start
        else (_flag or ""),
    )
    db = connect(cfg.state_path)
    man = S.normalize_manual(cfg.manual)
    _check_manual_modes(man)
    validate_manual(man)
    if man.get("legs", 1) != 1 or man.get("entry_mode", "Market") not in (
        "Market",
        "Pullback-Limit",
    ):
        raise SystemExit(
            "nur legs=1 + entry_mode Market/Pullback-Limit werden unterstuetzt (kein Gestaffelt)"
        )
    ex_map: dict = {}
    lev_ok: dict[str, bool] = {}
    warned: set[str] = set()
    err_sent: dict[str, float] = {}
    saved_hash: dict[str, str] = {}
    _init_exchanges(cfg, db, ex_map, lev_ok)
    audit(db, "engine", "start", f"mode={cfg.mode} symbols={len(cfg.symbols)}")
    if cfg.mode == "live":
        TG.send(f"Bot gestartet (LIVE): {len(cfg.symbols)} Symbole")
    _warn_unknown_manual(cfg)
    tf_min = {s.symbol: TF_MINUTES.get(s.timeframe, 60) for s in cfg.symbols}
    trend_state: dict[str, dict] = {}
    return {
        "cfg": cfg,
        "db": db,
        "risk": risk,
        "man": man,
        "ex_map": ex_map,
        "lev_ok": lev_ok,
        "warned": warned,
        "err_sent": err_sent,
        "saved_hash": saved_hash,
        "trend_state": trend_state,
        "tf_min": tf_min,
        "cycle_no": 0,
    }


def _poll_signal(ctx, ex, s, key):

    preset = get_preset(s.asset_preset, s.timeframe, ctx["cfg"].fine_tune)
    df = fetch_frame(ex, s.symbol, s.timeframe)
    if len(df) < 2:
        return None
    use_live_bars = (
        S.normalize_manual(ctx["cfg"].manual).get("timing", "Kerzenschluss")
        == "Echtzeit"
    )
    frame = df if use_live_bars else closed_frame(df)
    st = ctx["trend_state"].setdefault(key, {"trend": -1, "entries": 0})
    sig = evaluate(
        frame,
        preset,
        ctx["cfg"].manual or {},
        trend_entries=st["entries"] if st["trend"] != -1 else 0,
    )
    if sig["trend_no"] != st["trend"]:
        st["trend"] = sig["trend_no"]
        st["entries"] = 0
    price = (
        last_price(ex, s.symbol, sig["close"])
        if ctx["cfg"].mode == "live"
        else sig["close"]
    )
    forming = df.iloc[-1]
    ex_hi = max(float(forming["high"]), price)
    ex_lo = min(float(forming["low"]), price)

    return {
        "preset": preset,
        "sig": sig,
        "price": price,
        "ex_hi": ex_hi,
        "ex_lo": ex_lo,
    }


def _warn_unmanaged(ctx, s, key, cycle, live_qty) -> None:
    if (
        not cycle
        and ctx["cfg"].mode == "live"
        and live_qty != 0.0
        and key not in ctx["warned"]
    ):
        ctx["warned"].add(key)
        msg = f"Unverwaltete Position {s.exchange} {s.symbol} ({live_qty}) - Entries blockiert bis flat"
        log.error("unmanaged position", extra={"symbol": s.symbol})
        if ctx["cfg"].telegram.enabled:
            TG.send(msg)


def _reconcile_extern(ctx, ex, s, key, q):

    cycle = load_open(ctx["db"], s.exchange, s.symbol)
    # reconcile live exchange state
    live_qty = X.fetch_position_qty(ex, s.symbol) if ctx["cfg"].mode == "live" else 0.0
    if (
        cycle
        and ctx["cfg"].mode == "live"
        and live_qty == 0.0
        and not cycle.get("just_opened")
    ):
        # externally closed: finalize with estimate
        pnl = (
            tranche_pnl(
                cycle["side"],
                cycle["entry"],
                q["price"],
                sum(t["qty"] for t in cycle["tps"] if not t["hit"]),
                cycle["fee_pct"],
            )
            + cycle["realized"]
        )
        try:
            eqx = cached_equity(key, ex, s.symbol, ctx["cfg"].mode)
        except Exception:
            eqx = 10000.0
        finish_close(
            ctx["db"], ctx["risk"], ctx["cfg"], s, cycle, pnl, q["price"], "EXTERN", eqx
        )
        cycle = None
    _warn_unmanaged(ctx, s, key, cycle, live_qty)

    q["live_qty"] = live_qty
    q["cycle"] = cycle
    return q


def _poll_pending(ctx, ex, s, q) -> bool:

    if q["cycle"] and q["cycle"].get("pending"):
        if ctx["cfg"].mode == "live":
            _filled = q["live_qty"] != 0.0
        else:
            _a = float(q["cycle"].get("anchor", q["cycle"]["entry"]))
            _filled = (q["cycle"]["side"] == "long" and q["ex_lo"] <= _a) or (
                q["cycle"]["side"] == "short" and q["ex_hi"] >= _a
            )
        if _filled:
            q["cycle"].pop("pending", None)
        elif time.time() >= float(q["cycle"].get("deadline", 0)):
            if ctx["cfg"].mode == "live":
                from .orders import cancel_open as _cancel

                with suppress(Exception):
                    _cancel(ex, s.symbol)
            clear_open(ctx["db"], s.exchange, s.symbol)
            audit(
                ctx["db"],
                "engine",
                "expired",
                f"{s.exchange} {s.symbol} pullback-limit abgelaufen",
            )
            q["cycle"] = None
            return True
        else:
            return True

    return False


def _sync_entry_once(ctx, ex, s, cycle) -> None:

    # refresh entry from broker once (avg fill)
    if ctx["cfg"].mode == "live" and not cycle.get("entry_synced"):
        with suppress(Exception):
            for p in ex.fetch_positions([s.symbol]):
                ep = float(p.get("entryPrice") or 0)
                if ep > 0:
                    cycle["entry"] = ep
                    break
            cycle["entry_synced"] = True


def _apply_closes(ctx, ex, s, cycle, ev) -> None:

    for qty, px, reason in ev["closes"]:
        if not send_market_close(s.order_mode, reason, ctx["cfg"].mode == "live"):
            cycle["realized"] += tranche_pnl(
                cycle["side"], cycle["entry"], px, qty, cycle["fee_pct"]
            )
            continue  # exchange executes TP/SL natively; only book it
        rq = round_amount(ex, s.symbol, qty) if ctx["cfg"].mode == "live" else qty
        if ctx["cfg"].mode == "live":
            from .orders import close_position, client_id

            try:
                close_position(
                    ex,
                    s.symbol,
                    cycle["side"],
                    rq,
                    client_id(cycle["cycle"], 0, 7),
                )
            except Exception as e:
                log.error(
                    "close failed",
                    extra={"symbol": s.symbol, "error": str(e)},
                )
                continue
        cycle["realized"] += tranche_pnl(
            cycle["side"], cycle["entry"], px, qty, cycle["fee_pct"]
        )


def _apply_session_close(ctx, sig, ev, cycle, price) -> None:

    if (
        not ev["closed_all"]
        and ctx["man"].get("close_session", False)
        and not sig.get("session_ok", True)
    ):
        _rem = sum(t["qty"] for t in cycle["tps"] if not t["hit"])
        if _rem > 0:
            ev["closes"].append((_rem, price, "SESSION"))
        ev["closed_all"] = True
        ev["why"] = "SESSION"


def _settle_closed(ctx, ex, s, key, q, ev, price) -> bool:
    cycle = q["cycle"]
    if ev["closed_all"]:
        try:
            eq = cached_equity(key, ex, s.symbol, ctx["cfg"].mode)
        except Exception:
            eq = 10000.0
        finish_close(
            ctx["db"],
            ctx["risk"],
            ctx["cfg"],
            s,
            cycle,
            cycle["realized"],
            price,
            ev["why"],
            eq,
        )
        q["cycle"] = None
        return ev["why"] == "OPP_REVERSE"
    save_open_if_changed(ctx["db"], s.exchange, s.symbol, cycle, ctx["saved_hash"])
    return False


def _poll_open(ctx, ex, s, key, q) -> bool:
    cycle = q["cycle"]
    if cycle.pop("just_opened", False):
        save_open_if_changed(ctx["db"], s.exchange, s.symbol, cycle, ctx["saved_hash"])
        return False
    if ctx["cfg"].mode == "live" and not cycle.get("entry_synced"):
        _sync_entry_once(ctx, ex, s, cycle)
    sig = q["sig"]
    ev = monitor(
        cycle,
        q["price"],
        q["ex_hi"],
        q["ex_lo"],
        sig["atr"],
        sig["trend_line"],
        sig,
        ctx["man"] or {},
    )
    _apply_closes(ctx, ex, s, cycle, ev)
    if (
        s.order_mode == "exchange_native"
        and ev["new_stop"]
        and ctx["cfg"].mode == "live"
    ):
        move_native_stop(ex, s.symbol, cycle)
    elif (
        should_resize_native_stop(s.order_mode, ctx["cfg"].mode == "live", ev["closes"])
        and not ev["closed_all"]
    ):
        move_native_stop(ex, s.symbol, cycle)
    _apply_session_close(ctx, sig, ev, cycle, q["price"])
    return _settle_closed(ctx, ex, s, key, q, ev, q["price"])


def _entry_guards(ctx, s, key, q):

    if ctx["risk"].killed:
        return ""
    if ctx["cfg"].mode == "live" and ctx["lev_ok"].get(s.symbol) is False:
        return ""
    if ctx["cfg"].mode == "live" and q["live_qty"] != 0.0:
        return ""
    if not cooldown_ok(
        ctx["risk"], key, ctx["tf_min"][s.symbol], ctx["cfg"].risk.cooldown_bars
    ):
        return ""
    want = "long" if q["sig"]["long"] else ("short" if q["sig"]["short"] else "")
    if not want:
        return ""
    if not q["sig"].get("session_ok", True):
        return ""

    return want


def _plan_stop(ctx, ex, s, q, want, anchor):
    _tick = X.price_tick(ex, s.symbol)
    if q["preset"].get("basis", "Bollinger") == "Bollinger":
        _rev = q["sig"]["bb_dn"] if want == "long" else q["sig"]["bb_up"]
    else:
        _rev = q["sig"]["rng_lo"] if want == "long" else q["sig"]["rng_up"]
    _ref = {
        "trend_line": q["sig"]["trend_line"],
        "support": q["sig"]["support"],
        "resistance": q["sig"]["resistance"],
        "reverse": _rev,
        "tick": _tick,
        "pad_ticks": ctx["man"].get("stop_pad_ticks", 1),
    }
    try:
        _stop_raw = entry_prices(
            anchor,
            want,
            q["preset"].get("stop_mode", "Prozent"),
            q["preset"],
            q["sig"]["atr"],
            _ref,
        )
    except ValueError as e:
        log.error(
            "stop setup blocked",
            extra={"symbol": s.symbol, "error": str(e)},
        )
        return None
    stop = (
        round_price(ex, s.symbol, _stop_raw) if ctx["cfg"].mode == "live" else _stop_raw
    )
    if want == "long" and anchor <= stop or want == "short" and anchor >= stop:
        return None
    if _tick and abs(anchor - stop) < _tick:
        log.error("stop under one tick, blocked", extra={"symbol": s.symbol})
        return None

    return stop


def _plan_qty(ctx, ex, s, q, anchor, stop, equity):
    qtys, _, _, err = plan_qty(
        equity,
        anchor,
        1.0,
        (ctx["cfg"].manual or {}).get("sizing", "Stoprisiko"),
        float((ctx["cfg"].manual or {}).get("risk_pct", 1.0)),
        float((ctx["cfg"].manual or {}).get("capital_pct", 10.0)),
        s.leverage,
        ctx["cfg"].risk.max_position_value_pct,
        abs(anchor - stop),
        [1.0],
        1,
        int(q["preset"].get("tp_count", 4)),
        0.001,
    )
    if err or not qtys:
        log.error("sizing blocked", extra={"symbol": s.symbol, "error": err})
        return None
    qty = round_amount(ex, s.symbol, qtys[0]) if ctx["cfg"].mode == "live" else qtys[0]
    if qty <= 0:
        return None
    if not meets_min_notional(ex, s.symbol, qty, anchor, ctx["cfg"].mode == "live"):
        log.error(
            "under exchange minimum, skipped",
            extra={"symbol": s.symbol, "qty": qty},
        )
        return None

    return qty


def _entry_plan(ctx, ex, s, key, q, want):

    equity = cached_equity(key, ex, s.symbol, ctx["cfg"].mode)
    anchor = q["price"]
    _is_pullback = ctx["man"].get("entry_mode", "Market") == "Pullback-Limit"
    if _is_pullback:
        anchor = pullback_anchor(q["price"], want, q["sig"]["atr"], ctx["man"])

    stop = _plan_stop(ctx, ex, s, q, want, anchor)
    if stop is None:
        return None
    qty = _plan_qty(ctx, ex, s, q, anchor, stop, equity)
    if qty is None:
        return None
    return {
        "want": want,
        "anchor": anchor,
        "stop": stop,
        "qty": qty,
        "pullback": _is_pullback,
    }


def _place_live_orders(ctx, ex, s, cycle, want, qty, anchor, stop, pullback) -> bool:
    from .orders import place_entry, place_bracket, client_id

    fill = place_entry(
        ex,
        s.symbol,
        want,
        qty,
        anchor if pullback else None,
        client_id(ctx["cycle_no"], 0, 9),
    )
    try:
        cycle["entry"] = float(fill.get("average") or fill.get("price") or anchor)
    except Exception:
        cycle["entry"] = anchor
    if s.order_mode == "exchange_native":
        tps = [
            (
                round_price(ex, s.symbol, t["price"]),
                round_amount(ex, s.symbol, t["qty"]),
            )
            for t in cycle["tps"]
        ]
        try:
            placed = place_bracket(ex, s.symbol, want, qty, stop, tps, ctx["cycle_no"])
            if placed and placed[0].get("id"):
                cycle["stop_order_id"] = placed[0]["id"]
        except Exception as e:
            log.error(
                "bracket failed, closing entry",
                extra={"symbol": s.symbol, "error": str(e)},
            )
            from .orders import close_position

            with suppress(Exception):
                close_position(ex, s.symbol, want, qty)
            return False
    return True


def _entry_place(ctx, ex, s, key, q, plan) -> None:
    want = plan["want"]
    anchor = plan["anchor"]
    stop = plan["stop"]
    qty = plan["qty"]
    _is_pullback = plan["pullback"]

    ctx["cycle_no"] += 1
    cycle = build_cycle(
        ctx["cycle_no"],
        want,
        qty,
        anchor,
        stop,
        q["preset"],
        ctx["man"],
        q["sig"]["atr"],
    )
    if ctx["cfg"].mode == "live":
        if not _place_live_orders(
            ctx, ex, s, cycle, want, qty, anchor, stop, _is_pullback
        ):
            return None
    if _is_pullback:
        cycle["pending"] = True
        cycle["anchor"] = anchor
        cycle["deadline"] = (
            time.time()
            + int(ctx["man"].get("limit_life", 10)) * ctx["tf_min"][s.symbol] * 60
        )
    else:
        cycle["just_opened"] = True
    save_open(ctx["db"], s.exchange, s.symbol, cycle)
    ctx["trend_state"][key]["entries"] += 1
    audit(
        ctx["db"],
        "engine",
        "entry",
        f"{s.exchange} {s.symbol} {want} qty={qty} entry={anchor} stop={stop}",
    )
    if ctx["cfg"].telegram.enabled and (
        ctx["cfg"].telegram.notify_open
        if hasattr(ctx["cfg"].telegram, "notify_open")
        else True
    ):
        TG.trade_open(s.exchange, s.symbol, want, qty, anchor, stop)


def _poll_entry(ctx, ex, s, key, q) -> None:
    want = _entry_guards(ctx, s, key, q)
    if not want:
        return
    plan = _entry_plan(ctx, ex, s, key, q, want)
    if plan is None:
        return
    _entry_place(ctx, ex, s, key, q, plan)


def _poll_symbol(ctx, s) -> None:
    if not s.enabled:
        return
    ex = ctx["ex_map"][s.exchange]
    key = f"{s.exchange}:{s.symbol}"
    try:
        q = _poll_signal(ctx, ex, s, key)
        if q is None:
            return
        q = _reconcile_extern(ctx, ex, s, key, q)
        if q["cycle"] and q["cycle"].get("pending"):
            if _poll_pending(ctx, ex, s, q):
                return
        if q["cycle"]:
            if not _poll_open(ctx, ex, s, key, q):
                return
        _poll_entry(ctx, ex, s, key, q)
    except SystemExit:
        raise
    except Exception as e:
        log.error("symbol loop error", extra={"symbol": s.symbol, "error": str(e)})
        if (
            ctx["cfg"].telegram.enabled
            and ctx["cfg"].telegram.notify_errors
            and should_send_error(ctx["err_sent"], key, time.time())
        ):
            TG.send(f"FEHLER {s.exchange} {s.symbol}: {e}")


def run_loop(cfg: AppCfg, once: bool = False) -> None:
    ctx = _setup_run(cfg)
    while True:
        _flag = kill_flag_reason(ctx["cfg"].state_path)
        if _flag is not None and not ctx["risk"].killed:
            ctx["risk"].engage("flag: " + _flag)
        for s in ctx["cfg"].symbols:
            _poll_symbol(ctx, s)
        # auto kill-switch: close everything
        if ctx["risk"].killed and not getattr(run_loop, "_kill_closed", False):
            run_loop._kill_closed = True  # type: ignore[attr-defined]
            for s in ctx["cfg"].symbols:
                if s.enabled and ctx["cfg"].mode == "live":
                    from .orders import close_all

                    with suppress(Exception):
                        close_all(ctx["ex_map"][s.exchange], s.symbol)
            if ctx["cfg"].telegram.enabled and ctx["cfg"].telegram.notify_kill:
                TG.killed(ctx["risk"].kill_reason)
        if once:
            break
        time.sleep(max(5, ctx["cfg"].poll_seconds))


def cooldown_ok(risk: RiskState, key: str, tf_minutes: int, cooldown_bars: int) -> bool:

    last = risk.last_flat_bar.get(key)
    if last is None:
        return True
    return time.time() >= last + cooldown_bars * tf_minutes * 60


def finish_close(
    db,
    risk: RiskState,
    cfg: AppCfg,
    s,
    cycle: dict,
    pnl: float,
    price: float,
    why: str,
    equity: float = 10000.0,
) -> None:

    db.execute(
        "INSERT INTO cycles(ts,exchange,symbol,side,entry,stop,qty,pnl,note) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(),
            s.exchange,
            s.symbol,
            1 if cycle["side"] == "long" else -1,
            cycle["entry"],
            cycle["stop"],
            cycle["qty"],
            pnl,
            why,
        ),
    )
    db.commit()
    clear_open(db, s.exchange, s.symbol)
    risk.last_flat_bar[f"{s.exchange}:{s.symbol}"] = time.time()
    try:
        msg = risk.register_pnl(pnl, cfg.risk.max_daily_loss_pct, equity)
        if msg:
            log.error("kill switch tripped", extra={"reason": msg})
    except Exception as e:
        log.error("risk register failed", extra={"error": str(e)})
    audit(
        db,
        "engine",
        "close",
        f"{s.exchange} {s.symbol} {cycle['side']} pnl={pnl:.2f} why={why} @ {price}",
    )
    if cfg.telegram.enabled and cfg.telegram.notify_close:
        TG.trade_close(s.exchange, s.symbol, pnl, why)


def _cancel_tracked_stop(ex, symbol: str, cycle: dict) -> bool:
    if cycle.get("stop_order_id"):
        with suppress(Exception):
            ex.cancel_order(cycle["stop_order_id"], symbol)
            return True
    return False


def move_native_stop(ex, symbol: str, cycle: dict) -> None:
    """Move exchange-native SL after BE/trailing: cancel tracked stop, place new one."""
    from .orders import stop_market_type, client_id

    try:
        side = cycle["side"]
        exit_side = "sell" if side == "long" else "buy"
        if not _cancel_tracked_stop(ex, symbol, cycle):
            for o in ex.fetch_open_orders(symbol):
                blob = str(o.get("type", "")) + str(o.get("info", ""))
                if "stop" in blob.lower() or "trigger" in blob.lower():
                    with suppress(Exception):
                        ex.cancel_order(o["id"], symbol)
        live = abs(X.fetch_position_qty(ex, symbol))
        remaining = (
            live if live > 0 else sum(t["qty"] for t in cycle["tps"] if not t["hit"])
        )
        if remaining > 0:
            oid = ex.create_order(
                symbol,
                stop_market_type(ex),
                exit_side,
                remaining,
                None,
                {
                    "stopPrice": cycle["stop"],
                    "reduceOnly": True,
                    "clientOrderId": client_id(cycle["cycle"], 0, 8),
                },
            )
            if isinstance(oid, dict) and oid.get("id"):
                cycle["stop_order_id"] = oid["id"]
    except Exception as e:
        log.error("move native stop failed", extra={"symbol": symbol, "error": str(e)})
