"""Signal core: trendline/breakout, filters, oscillator, TP/BE/trailing math (Pine parity)."""
from __future__ import annotations
import pandas as pd
from . import indicators as I

def compute_frame(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    out = df.copy()
    bb_len = int(p.get("bb_len", 100)); bb_dev = float(p.get("bb_dev", 1.5))
    range_len = int(p.get("range_len", 200))
    fib_u = float(p.get("fib_upper", 0.236)); fib_l = float(p.get("fib_lower", 0.786))
    smooth = int(p.get("smooth_len", 3)); atr_len = int(p.get("atr_len", 14))
    regime_len = int(p.get("regime_len", 100))
    adapt_min = float(p.get("adapt_min", 0.5)); adapt_max = float(p.get("adapt_max", 2.0))
    f_atr_len = int(p.get("filter_atr_len", 5)); f_ma_len = int(p.get("filter_ma_len", 5))
    f_ma = p.get("filter_ma_type", "EMA"); rsi_len = int(p.get("rsi_len", 7))
    o_rsi = int(p.get("osc_rsi_len", 14)); o_st = int(p.get("osc_stoch_len", 14))
    o_k = int(p.get("osc_k_len", 3)); o_d = int(p.get("osc_d_len", 3))
    vol_len = int(p.get("volume_len", 20))
    out["mid"] = I.sma(out["close"], bb_len)
    out["sd"] = out["close"].rolling(bb_len, min_periods=bb_len).std(ddof=0)
    out["atr"] = I.atr(out["high"], out["low"], out["close"], atr_len)
    out["atr_mean"] = out["atr"].rolling(regime_len, min_periods=regime_len).mean()
    out["regime"] = (out["atr"] / out["atr_mean"]).clip(adapt_min, adapt_max).fillna(1.0)
    adapt = out["close"] * 0 + (1.0 if not p.get("adaptive_offset") else out["regime"])
    out["bb_up"] = out["mid"] + out["sd"] * bb_dev * adapt
    out["bb_dn"] = out["mid"] - out["sd"] * bb_dev * adapt
    out["rh"] = I.highest(out["high"], range_len)
    out["rl"] = I.lowest(out["low"], range_len)
    out["rw"] = out["rh"] - out["rl"]
    out["upper"] = out["rh"] - out["rw"] * fib_u
    out["lower"] = out["rh"] - out["rw"] * fib_l
    out["f_atr"] = I.atr(out["high"], out["low"], out["close"], f_atr_len)
    fe, fs = I.ema(out["f_atr"], f_ma_len), out["f_atr"].rolling(f_ma_len, min_periods=f_ma_len).mean()
    out["f_avg"] = fe if f_ma == "EMA" else fs
    out["rsi"] = I.rsi_floor(out["close"], rsi_len)
    out["vol_avg"] = out["volume"].rolling(vol_len, min_periods=vol_len).mean() if "volume" in out else float("nan")
    out["osc_rsi"] = I.rsi_floor(out["close"], o_rsi)
    k = I.stoch_k(out["osc_rsi"], out["osc_rsi"], out["osc_rsi"], o_st)
    out["osc_k"] = k.rolling(o_k, min_periods=o_k).mean()
    out["osc_d"] = out["osc_k"].rolling(o_d, min_periods=o_d).mean()
    out["ema_low"] = I.ema(out["low"], smooth)
    out["ema_high"] = I.ema(out["high"], smooth)
    out["ad_low"] = I.adaptive_series(out["low"], out["regime"], smooth)
    out["ad_high"] = I.adaptive_series(out["high"], out["regime"], smooth)
    return out

def _refs(style: str, l: float, h: float, el: float, eh: float, al: float, ah: float) -> tuple[float, float]:
    if style == "Sharp":
        return l, h
    if style == "Balanced":
        return (el if el == el else l), (eh if eh == eh else h)
    # Smooth: regime-adaptive filter (Pine adaptiveLow/adaptiveHigh)
    return (al if al == al else l), (ah if ah == ah else h)

def trend_signals(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    out = df.copy()
    basis, style = p.get("basis", "Bollinger"), p.get("style", "Sharp")
    buf_on, buf_mult = bool(p.get("trend_buffer_on")), float(p.get("trend_buffer_mult", 1.0))
    lines: list[float] = []; dirs: list[int] = []
    prev_line, prev_dir = float("nan"), 0
    turns_l, turns_s = [], []
    for i in range(len(out)):
        th_u = out["bb_up"].iloc[i] if basis == "Bollinger" else out["upper"].iloc[i]
        th_d = out["bb_dn"].iloc[i] if basis == "Bollinger" else out["lower"].iloc[i]
        c, h, l = out["close"].iloc[i], out["high"].iloc[i], out["low"].iloc[i]
        brk = 1 if c > th_u else (-1 if c < th_d else 0)
        lc, hc = _refs(style, l, h, out["ema_low"].iloc[i], out["ema_high"].iloc[i],
                        out["ad_low"].iloc[i], out["ad_high"].iloc[i])
        buf = out["atr"].iloc[i] * buf_mult if buf_on else 0.0
        if prev_line != prev_line:
            prev_line = lc if brk == 1 else (hc if brk == -1 else c)
        if brk == 1:
            line = max(prev_line, lc - buf)
        elif brk == -1:
            line = min(prev_line, hc + buf)
        else:
            line = prev_line
        d = 1 if line > prev_line else (-1 if line < prev_line else prev_dir)
        tl = prev_dir == -1 and d == 1
        ts = prev_dir == 1 and d == -1
        lines.append(line); dirs.append(d); turns_l.append(tl); turns_s.append(ts)
        prev_line, prev_dir = line, d
    out["trend_line"], out["trend_dir"] = lines, dirs
    out["turn_long"], out["turn_short"] = turns_l, turns_s
    out["osc_cross_up"] = I.crossover(out["osc_k"], out["osc_d"]) & (out[["osc_k"]].min(axis=1) <= float(p.get("osc_low", 20)))
    out["osc_cross_dn"] = I.crossunder(out["osc_k"], out["osc_d"]) & (out[["osc_k"]].max(axis=1) >= float(p.get("osc_high", 80)))
    return out

def trigger_hit(trigger: str, hits: list[bool], rr: float, thr: float) -> bool:
    if trigger == "Sofort":
        return True
    if trigger == "R":
        return rr >= thr
    for idx, name in enumerate(["TP1", "TP2", "TP3", "TP4"]):
        if trigger == name and idx < len(hits) and hits[idx]:
            return True
    return False

def tp_distances(anchor: float, stop: float, atr_v: float, p: dict, tp_count: int) -> list[float]:
    basis = p.get("tp_basis", "Prozent")
    outs = []
    for j in range(tp_count):
        v = float(p.get(f"tp{j+1}", 2.0))
        d = anchor * v / 100 if basis == "Prozent" else (atr_v * v if basis == "ATR" else abs(anchor - stop) * v)
        outs.append(max(d, 1e-9))
    return outs
