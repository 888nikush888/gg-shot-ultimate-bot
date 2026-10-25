"""Signal core: trendline/breakout, filters, oscillator, TP/BE/trailing math (Pine parity)."""

from __future__ import annotations
from contextlib import suppress
import numpy as np
import pandas as pd
from . import indicators as I
from .indicators import _is_na

_ALIASES = {
    "oscMode": "osc_mode",
    "oscEntries": "osc_entries",
    "oscReentries": "osc_reentries",
    "oscExit": "osc_exit",
    "oscAfter": "osc_exit_after",
    "oscLow": "osc_low",
    "oscHigh": "osc_high",
    "oscRsiLen": "osc_rsi_len",
    "oscStochLen": "osc_stoch_len",
    "oscKLen": "osc_k_len",
    "oscDLen": "osc_d_len",
    "reentryOn": "reentry_on",
    "maxReentries": "max_reentries",
    "bounceOn": "bounce_on",
    "srBounceOn": "sr_bounce_on",
    "entryMode": "entry_mode",
    "entryUnit": "entry_unit",
    "pullbackDistance": "pullback_distance",
    "limitLife": "limit_life",
    "tradeSession": "trade_session",
    "sessionZone": "session_zone",
    "sessionOn": "session_on",
    "closeSession": "close_session",
    "zoneAtr": "zone_atr",
    "divAge": "div_age",
    "divMode": "div_mode",
    "pivotLen": "pivot_len",
    "allowLong": "allow_long",
    "allowShort": "allow_short",
    "startDate": "date_start",
    "endDate": "date_end",
}


def normalize_manual(manual: dict | None) -> dict:
    """Merge Pine camelCase keys into snake_case (snake wins). Never mutates input."""
    m = dict(manual or {})
    for camel, snake in _ALIASES.items():
        if camel in m and snake not in m:
            m[snake] = m[camel]
    return {k: v for k, v in m.items() if v is not None}


def mval(manual: dict, *names, default=None):
    for n in names:
        if n in manual:
            return manual[n]
    return default


def compute_frame(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    out = df.copy()
    bb_len = int(p.get("bb_len", 100))
    bb_dev = float(p.get("bb_dev", 1.5))
    range_len = int(p.get("range_len", 200))
    fib_u = float(p.get("fib_upper", 0.236))
    fib_l = float(p.get("fib_lower", 0.786))
    smooth = int(p.get("smooth_len", 3))
    atr_len = int(p.get("atr_len", 14))
    regime_len = int(p.get("regime_len", 100))
    adapt_min = float(p.get("adapt_min", 0.5))
    adapt_max = float(p.get("adapt_max", 2.0))
    f_atr_len = int(p.get("filter_atr_len", 5))
    f_ma_len = int(p.get("filter_ma_len", 5))
    f_ma = p.get("filter_ma_type", "EMA")
    rsi_len = int(p.get("rsi_len", 7))
    o_rsi = int(p.get("osc_rsi_len", 14))
    o_st = int(p.get("osc_stoch_len", 14))
    o_k = int(p.get("osc_k_len", 3))
    o_d = int(p.get("osc_d_len", 3))
    vol_len = int(p.get("volume_len", 20))
    out.loc[:, "mid"] = I.sma(out["close"], bb_len)
    out.loc[:, "sd"] = out["close"].rolling(bb_len, min_periods=bb_len).std(ddof=0)
    out.loc[:, "atr"] = I.atr(out["high"], out["low"], out["close"], atr_len)
    out.loc[:, "atr_mean"] = (
        out["atr"].rolling(regime_len, min_periods=regime_len).mean()
    )
    out.loc[:, "regime"] = (
        (out["atr"] / out["atr_mean"]).clip(adapt_min, adapt_max).fillna(1.0)
    )
    adapt = out["close"] * 0 + (1.0 if not p.get("adaptive_offset") else out["regime"])
    out.loc[:, "bb_up"] = out["mid"] + out["sd"] * bb_dev * adapt
    out.loc[:, "bb_dn"] = out["mid"] - out["sd"] * bb_dev * adapt
    out.loc[:, "rh"] = I.highest(out["high"], range_len)
    out.loc[:, "rl"] = I.lowest(out["low"], range_len)
    out.loc[:, "rw"] = out["rh"] - out["rl"]
    out.loc[:, "upper"] = out["rh"] - out["rw"] * fib_u
    out.loc[:, "lower"] = out["rh"] - out["rw"] * fib_l
    out.loc[:, "f_atr"] = I.atr(out["high"], out["low"], out["close"], f_atr_len)
    fe, fs = (
        I.ema(out["f_atr"], f_ma_len),
        out["f_atr"].rolling(f_ma_len, min_periods=f_ma_len).mean(),
    )
    out.loc[:, "f_avg"] = fe if f_ma == "EMA" else fs
    out.loc[:, "rsi"] = I.rsi_floor(out["close"], rsi_len)
    out.loc[:, "vol_avg"] = (
        out["volume"].rolling(vol_len, min_periods=vol_len).mean()
        if "volume" in out
        else float("nan")
    )
    out.loc[:, "osc_rsi"] = I.rsi_floor(out["close"], o_rsi)
    k = I.stoch_k(out["osc_rsi"], out["osc_rsi"], out["osc_rsi"], o_st)
    out.loc[:, "osc_k"] = k.rolling(o_k, min_periods=o_k).mean()
    out.loc[:, "osc_d"] = out["osc_k"].rolling(o_d, min_periods=o_d).mean()
    out.loc[:, "ema_low"] = I.ema(out["low"], smooth)
    out.loc[:, "ema_high"] = I.ema(out["high"], smooth)
    out.loc[:, "ad_low"] = I.adaptive_series(out["low"], out["regime"], smooth)
    out.loc[:, "ad_high"] = I.adaptive_series(out["high"], out["regime"], smooth)
    piv = int(p.get("pivot_len", 5))
    out.loc[:, "piv_low"] = I.pivotlow(out["low"], piv)
    out.loc[:, "piv_high"] = I.pivothigh(out["high"], piv)
    return out


def _refs(
    style: str, low: float, high: float, el: float, eh: float, al: float, ah: float
) -> tuple[float, float]:
    if style == "Sharp":
        return low, high
    if style == "Balanced":
        return (el if not _is_na(el) else low), (eh if not _is_na(eh) else high)
    # Smooth: regime-adaptive filter (Pine adaptiveLow/adaptiveHigh)
    return (al if not _is_na(al) else low), (ah if not _is_na(ah) else high)


def _div_hit(bull, piv_v, prior_v, rsi_v, prior_rsi_v) -> bool:
    if _is_na(prior_v) or _is_na(rsi_v) or _is_na(prior_rsi_v):
        return False
    if bull:
        return piv_v < prior_v and rsi_v > prior_rsi_v
    return piv_v > prior_v and rsi_v < prior_rsi_v


def _div_track(piv_v, rsi_v, prior_v, prior_rsi_v, last_ev, idx, bull):
    if _div_hit(bull, piv_v, prior_v, rsi_v, prior_rsi_v):
        return idx, piv_v, rsi_v
    return last_ev, piv_v, rsi_v


def div_supres(df: pd.DataFrame, p: dict) -> tuple:
    """Pine pivot divergences + S/R levels. Returns (bull_age, bear_age, sup, res) arrays."""
    piv = int(p.get("pivot_len", 5))
    rsi = df["rsi"].to_numpy(dtype=float)
    pl = df["piv_low"].to_numpy(dtype=float)
    ph = df["piv_high"].to_numpy(dtype=float)
    n = len(df)
    bull_age = np.full(n, np.inf)
    bear_age = np.full(n, np.inf)
    sup = np.full(n, np.nan)
    res = np.full(n, np.nan)
    prior_pl = np.nan
    prior_pl_rsi = np.nan
    prior_ph = np.nan
    prior_ph_rsi = np.nan
    last_bull = float("-inf")
    last_bear = float("-inf")
    cur_sup = np.nan
    cur_res = np.nan
    for i in range(n):
        if not _is_na(pl[i]):
            prsi = rsi[i - piv] if i - piv >= 0 else np.nan
            last_bull, prior_pl, prior_pl_rsi = _div_track(
                pl[i], prsi, prior_pl, prior_pl_rsi, last_bull, i, True
            )
            cur_sup = pl[i]
        if not _is_na(ph[i]):
            prsi = rsi[i - piv] if i - piv >= 0 else np.nan
            last_bear, prior_ph, prior_ph_rsi = _div_track(
                ph[i], prsi, prior_ph, prior_ph_rsi, last_bear, i, False
            )
            cur_res = ph[i]
        bull_age[i] = i - last_bull
        bear_age[i] = i - last_bear
        sup[i] = cur_sup
        res[i] = cur_res
    return bull_age, bear_age, sup, res


def session_allows(now_utc, manual: dict) -> bool:
    """Pine session filter: days (1=Sunday) + HHMM-HHMM range, 0000-0000 = all day."""
    if not manual.get("session_on", False):
        return True
    with suppress(Exception):
        import zoneinfo

        now_utc = now_utc.astimezone(
            zoneinfo.ZoneInfo(str(manual.get("session_zone", "Etc/UTC")))
        )
    days = manual.get("session_days", [1, 2, 3, 4, 5, 6, 7])
    if now_utc.isoweekday() % 7 + 1 not in days:
        return False
    sess = str(manual.get("trade_session", "0000-0000"))
    try:
        a, b = sess.split("-")
        ah, am, bh, bm = int(a[:2]), int(a[2:]), int(b[:2]), int(b[2:])
    except Exception:
        return True
    if (ah, am) == (bh, bm):
        return True
    cur = (now_utc.hour, now_utc.minute)
    if (ah, am) <= (bh, bm):
        return (ah, am) <= cur < (bh, bm)
    return cur >= (ah, am) or cur < (bh, bm)


def date_allows(now_utc, manual: dict) -> bool:
    try:
        import datetime as _dt

        start = _dt.datetime.fromisoformat(
            str(manual.get("date_start", "2020-01-01"))
        ).replace(tzinfo=_dt.timezone.utc)
        end = _dt.datetime.fromisoformat(
            str(manual.get("date_end", "2099-01-01"))
        ).replace(tzinfo=_dt.timezone.utc)
        return start <= now_utc <= end
    except Exception:
        return True


def pending_state(
    is_live: bool, live_qty: float, touched: bool, now_ts: float, deadline_ts: float
) -> str:
    """Pending pullback-limit entry: filled | waiting | expired."""
    if is_live:
        if live_qty:
            return "filled"
    elif touched:
        return "filled"
    return "expired" if now_ts >= deadline_ts else "waiting"


def _trend_seed(prev_line, brk, lc, hc, c):
    if _is_na(prev_line):
        if brk == 1:
            return lc
        if brk == -1:
            return hc
        return c
    return prev_line


def _trend_line(prev_line, prev_dir, brk, lc, hc, buf, c):
    # Linien-Update je Bar: gibt (line, d, tl, ts, turned) zurueck.
    prev_line = _trend_seed(prev_line, brk, lc, hc, c)
    if brk == 1:
        line = max(prev_line, lc - buf)
    elif brk == -1:
        line = min(prev_line, hc + buf)
    else:
        line = prev_line
    if line > prev_line:
        d = 1
    elif line < prev_line:
        d = -1
    else:
        d = prev_dir
    tl = prev_dir == -1 and d == 1
    ts = prev_dir == 1 and d == -1
    return line, d, tl, ts, (tl or ts) and d != prev_dir


def _channel_turns(touched, lo, lower, c, h, upper):
    tl = (not touched) and (lo <= lower < c)
    ts = (not touched) and (c < upper <= h)
    return tl, ts


def _trend_touch(d, c, h, lo, line, sup_v, res_v, atr_v, zone, bounce_on, sr_on):
    # Bounce/S-R-Touch je Bar: gibt (touch_l, touch_s) zurueck.
    touch_l = (d == 1) and (
        (bounce_on and lo <= line < c)
        or (
            sr_on
            and not _is_na(sup_v)
            and not _is_na(atr_v)
            and lo <= sup_v + atr_v * zone < c
        )
    )
    touch_s = (d == -1) and (
        (bounce_on and c < line <= h)
        or (
            sr_on
            and not _is_na(res_v)
            and not _is_na(atr_v)
            and c < res_v - atr_v * zone <= h
        )
    )
    return touch_l, touch_s


def _bounce_edge(adv, touch_l, touch_s, prev_l, prev_s, bounce_l, bounce_s):
    bounce_l.append(adv and touch_l and not prev_l)
    bounce_s.append(adv and touch_s and not prev_s)
    return touch_l, touch_s


def _trend_thresholds(basis, bb_up, bb_dn, upper, lower):
    if basis == "Bollinger":
        return bb_up, bb_dn
    return upper, lower


def _trend_break(c, th_u, th_d):
    if c > th_u:
        return 1
    if c < th_d:
        return -1
    return 0


def trend_signals(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    out = df.copy()
    basis, style = p.get("basis", "Bollinger"), p.get("style", "Sharp")
    buf_on, buf_mult = (
        bool(p.get("trend_buffer_on")),
        float(p.get("trend_buffer_mult", 1.0)),
    )
    bull_age, bear_age, sup, res = div_supres(out, p)
    out.loc[:, "bull_div_age"] = bull_age
    out.loc[:, "bear_div_age"] = bear_age
    out.loc[:, "support"] = sup
    out.loc[:, "resistance"] = res
    mode = p.get("mode", "Standard")
    adv = mode == "Advanced"
    bounce_on = bool(p.get("bounce_on", False))
    sr_on = bool(p.get("sr_bounce_on", False))
    zone = float(p.get("zone_atr", 0.25))
    lines: list[float] = []
    dirs: list[int] = []
    turns_l: list[bool] = []
    turns_s: list[bool] = []
    bounce_l: list[bool] = []
    bounce_s: list[bool] = []
    tnums: list[int] = []
    prev_line, prev_dir, tnum = float("nan"), 0, 0
    prev_touch_l, prev_touch_s = False, False
    for i in range(len(out)):
        th_u, th_d = _trend_thresholds(
            basis,
            out["bb_up"].iloc[i],
            out["bb_dn"].iloc[i],
            out["upper"].iloc[i],
            out["lower"].iloc[i],
        )
        c, h, lo = out["close"].iloc[i], out["high"].iloc[i], out["low"].iloc[i]
        atr_v = out["atr"].iloc[i]
        brk = _trend_break(c, th_u, th_d)
        lc, hc = _refs(
            style,
            lo,
            h,
            out["ema_low"].iloc[i],
            out["ema_high"].iloc[i],
            out["ad_low"].iloc[i],
            out["ad_high"].iloc[i],
        )
        buf = atr_v * buf_mult if buf_on else 0.0
        line, d, tl, ts, turned = _trend_line(prev_line, prev_dir, brk, lc, hc, buf, c)
        if turned:
            tnum += 1
        if mode == "Channel":
            lower, upper = out["lower"].iloc[i], out["upper"].iloc[i]
            touched = (lo <= lower) and (h >= upper)
            tl, ts = _channel_turns(touched, lo, lower, c, h, upper)
        touch_l, touch_s = _trend_touch(
            d, c, h, lo, line, sup[i], res[i], atr_v, zone, bounce_on, sr_on
        )
        prev_touch_l, prev_touch_s = _bounce_edge(
            adv, touch_l, touch_s, prev_touch_l, prev_touch_s, bounce_l, bounce_s
        )
        lines.append(line)
        dirs.append(d)
        turns_l.append(tl)
        turns_s.append(ts)
        tnums.append(tnum)
        prev_line, prev_dir = line, d
    out.loc[:, "trend_line"] = lines
    out.loc[:, "trend_dir"] = dirs
    out.loc[:, "turn_long"] = turns_l
    out.loc[:, "turn_short"] = turns_s
    out.loc[:, "bounce_long"] = bounce_l
    out.loc[:, "bounce_short"] = bounce_s
    out.loc[:, "trend_no"] = tnums
    out.loc[:, "osc_cross_up"] = I.crossover(out["osc_k"], out["osc_d"]) & (
        out[["osc_k"]].min(axis=1) <= float(p.get("osc_low", 20))
    )
    out.loc[:, "osc_cross_dn"] = I.crossunder(out["osc_k"], out["osc_d"]) & (
        out[["osc_k"]].max(axis=1) >= float(p.get("osc_high", 80))
    )
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


def tp_distances(
    anchor: float, stop: float, atr_v: float, p: dict, tp_count: int
) -> list[float]:
    basis = p.get("tp_basis", "Prozent")
    tp_count = max(1, min(4, int(tp_count)))
    outs: list[float] = []
    for j in range(tp_count):
        v = float(p.get(f"tp{j + 1}", 2.0))
        if basis == "Prozent":
            d = anchor * v / 100
        elif basis == "ATR":
            d = atr_v * v
        else:
            d = abs(anchor - stop) * v
        outs.append(max(d, 1e-9))
    return outs
