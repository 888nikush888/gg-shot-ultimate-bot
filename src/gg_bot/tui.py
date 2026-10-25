"""Terminal dashboard (Textual): the single interactive layer over the bot."""

from __future__ import annotations
import asyncio
import hashlib
import os
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

if TYPE_CHECKING:
    from .risk import RiskState

SALT = "gg-shot-ultimate-v6"


def hash_password(pw: str, salt_hex: str | None = None) -> str:
    """PBKDF2-Hash im Format pbkdf2$<salt>$<hash> (200k Runden, stdlib only)."""
    import secrets

    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), SALT.encode() + salt, 200_000)
    return "pbkdf2$" + salt.hex() + "$" + dk.hex()


def _legacy_hash(pw: str) -> str:
    return hashlib.sha256((SALT + ":" + pw).encode()).hexdigest()


def verify_password(pw: str) -> bool:
    want = os.getenv("DASHBOARD_PASSWORD_HASH", "")
    if not want or not pw:
        return False
    if want.startswith("pbkdf2$"):
        try:
            _, salt_hex, expect = want.split("$")
        except ValueError:
            return False
        return len(expect) == 64 and hash_password(pw, salt_hex) == want
    # Legacy: reiner SHA256-Hex (altes Format, weiter gueltig)
    return len(want) == 64 and _legacy_hash(pw) == want


CSS = """
#confirm-box { align: center middle; width: 60; height: auto; border: solid red; padding: 1 2; }
DataTable { height: 1fr; }
TabbedContent { height: 1fr; }
"""


class ConfirmModal(ModalScreen[bool]):
    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._message)
            with Horizontal():
                yield Button("Confirm", id="yes", variant="error")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class DashboardApp(App):
    TITLE = "GG-Shot Ultimate v6"
    CSS = CSS

    def __init__(self, config_path: str) -> None:
        super().__init__()
        self._config_path = config_path
        self._risk: RiskState | None = None
        self._ex_cache: dict = {}
        self._qty_cache: dict = {}
        self.reload_config()

    def reload_config(self) -> None:
        from .config import load_config
        from .risk import RiskState

        self.cfg = load_config(self._config_path)
        if self._risk is None:
            self._risk = RiskState()

    @staticmethod
    def compose() -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("Overview", id="tab-overview"):
                yield Static("", id="ov-summary")
                yield DataTable(id="ov-table")
            with TabPane("Positions", id="tab-positions"):
                yield DataTable(id="pos-table")
                with Horizontal():
                    yield Button("Refresh", id="pos-refresh")
                    yield Button("Close all", id="pos-closeall", variant="error")
            with TabPane("Presets", id="tab-presets"):
                with Horizontal():
                    yield Select([], id="ps-symbol")
                    yield Input(placeholder="filter, e.g. btc", id="ps-filter")
                    yield Button("List", id="ps-list")
                yield DataTable(id="ps-table")
                with Horizontal():
                    yield Select([], id="ps-asset")
                    yield Select(
                        [("15m", "15m"), ("30m", "30m"), ("1h", "1h")],
                        id="ps-tf",
                        value="1h",
                    )
                    yield Checkbox("Finetune", id="ps-finetune")
                    yield Button("Preview", id="ps-preview")
                    yield Button("Apply", id="ps-apply", variant="primary")
                yield DataTable(id="ps-preview")
            with TabPane("Control", id="tab-control"):
                yield Static("", id="ctl-state")
                with Horizontal():
                    yield Button("Switch mode", id="ctl-mode")
                    yield Select([], id="ctl-symbol")
                    yield Button("Enable", id="ctl-enable")
                    yield Button("Disable", id="ctl-disable")
                with Horizontal():
                    yield Button("Kill engage", id="ctl-kill", variant="error")
                    yield Button("Kill reset", id="ctl-unblock")
                    yield Button("Telegram test", id="ctl-tg")
                yield Static("", id="ctl-msg")
            with TabPane("Backtest", id="tab-backtest"):
                with Horizontal():
                    yield Select([], id="bt-symbol")
                    yield Input(
                        placeholder="limit, e.g. 300", id="bt-limit", value="300"
                    )
                    yield Button("Run", id="bt-run", variant="primary")
                yield Static("", id="bt-result")
            with TabPane("Events", id="tab-events"):
                yield DataTable(id="ev-table")
        yield Footer()

    def on_mount(self) -> None:
        self._fill_selects()
        self.refresh_all()
        self.set_interval(5.0, self.refresh_all)

    # ---------- data helpers ----------
    def _db(self):
        from .state import connect

        return connect(self.cfg.state_path)

    def _open_cycles(self) -> list[dict]:
        from .state import list_open_cycles

        db = self._db()
        try:
            return list_open_cycles(db)
        finally:
            db.close()

    def _fill_selects(self) -> None:
        sym_opts = [
            (f"{s.exchange} {s.symbol}", f"{s.exchange}|{s.symbol}")
            for s in self.cfg.symbols
        ]
        for wid in ("ps-symbol", "ctl-symbol", "bt-symbol"):
            sel = self.query_one("#" + wid, Select)
            sel.set_options(sym_opts)
        from .presets import list_presets

        assets = sorted({k.rsplit("_", 1)[0] for k in list_presets() if "_" in k})
        self.query_one("#ps-asset", Select).set_options([(a, a) for a in assets])
        self.query_one("#ps-finetune", Checkbox).value = bool(self.cfg.fine_tune)

    def _selected_symbol(self, wid: str):
        val = self.query_one("#" + wid, Select).value
        if val == Select.BLANK or not val:
            return None
        ex, _, sym = str(val).partition("|")
        for s in self.cfg.symbols:
            if s.exchange == ex and s.symbol == sym:
                return s
        return None

    # ---------- refresh ----------
    def refresh_all(self) -> None:
        try:
            self.reload_config()
        except Exception:
            return
        self.refresh_overview()
        self.refresh_positions()
        self.refresh_events()
        self.refresh_control()

    def refresh_overview(self) -> None:
        from .state import load_open

        db = self._db()
        try:
            n_open = len(db.execute("SELECT 1 FROM open_cycles").fetchall())
            n_cycles = db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
            opens = {
                (s.exchange, s.symbol): load_open(db, s.exchange, s.symbol)
                for s in self.cfg.symbols
            }
        finally:
            db.close()
        self.query_one("#ov-summary", Static).update(
            f"mode={self.cfg.mode} symbols={len(self.cfg.symbols)} "
            f"open={n_open} closed_cycles={n_cycles} killed={self._risk.killed}"
        )
        t = self.query_one("#ov-table", DataTable)
        t.clear(columns=True)
        for c in ["exchange", "symbol", "preset", "tf", "lev", "mode", "on", "pos"]:
            t.add_column(c)
        for s in self.cfg.symbols:
            oc = opens.get((s.exchange, s.symbol))
            pos = oc["side"] if oc else "-"
            t.add_row(
                s.exchange,
                s.symbol,
                s.asset_preset,
                s.timeframe,
                str(s.leverage),
                s.order_mode,
                "Y" if s.enabled else "n",
                pos,
            )

    def _live_qty(self, exchange: str, symbol: str) -> str:
        from . import exchanges as X

        now = time.time()
        hit = self._qty_cache.get(exchange + ":" + symbol)
        if hit and now - hit[0] < 30.0:
            return hit[1]
        if exchange not in self._ex_cache:
            try:
                self._ex_cache[exchange] = X.build(exchange, need_auth=True)
            except Exception:
                try:
                    self._ex_cache[exchange] = X.build(exchange, need_auth=False)
                except Exception:
                    return "n/a"
        ex = self._ex_cache[exchange]
        try:
            q = X.fetch_position_qty(ex, symbol)
            out = str(q)
        except Exception:
            return "n/a"
        self._qty_cache[exchange + ":" + symbol] = (now, out)
        return out

    def refresh_positions(self) -> None:
        t = self.query_one("#pos-table", DataTable)
        t.clear(columns=True)
        for c in [
            "exchange",
            "symbol",
            "side",
            "qty",
            "entry",
            "stop",
            "tp_hit",
            "realized",
            "live_qty",
        ]:
            t.add_column(c)
        for oc in self._open_cycles():
            p = oc["payload"]
            tps = p.get("tps", [])
            hits = sum(1 for x in tps if x.get("hit"))
            t.add_row(
                oc["exchange"],
                oc["symbol"],
                str(p.get("side")),
                str(round(float(p.get("qty", 0)), 6)),
                str(p.get("entry")),
                str(round(float(p.get("stop", 0)), 4)),
                f"{hits}/{len(tps)}",
                f"{float(p.get('realized', 0.0)):.2f}",
                self._live_qty(oc["exchange"], oc["symbol"]),
            )

    def refresh_events(self) -> None:
        db = self._db()
        try:
            rows = db.execute(
                "SELECT ts, actor, action, detail FROM audit ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
        finally:
            db.close()
        t = self.query_one("#ev-table", DataTable)
        t.clear(columns=True)
        for c in ["ts", "actor", "action", "detail"]:
            t.add_column(c)
        for r in rows:
            t.add_row(*[str(x)[:60] for x in r])

    def refresh_control(self) -> None:
        self.query_one("#ctl-state", Static).update(
            f"mode={self.cfg.mode} killed={self._risk.killed} reason={self._risk.kill_reason}"
        )

    # ---------- actions ----------
    async def _confirm(self, message: str) -> bool:
        return await self.push_screen(ConfirmModal(message))

    def _save(self, note: str = "Restart engine to apply.") -> None:
        from .config import save_config

        save_config(self.cfg, self._config_path)
        os.environ["BOT_MODE"] = self.cfg.mode
        self.reload_config()
        with suppress(Exception):
            self.notify(note)

    async def _flow_close_all(self) -> None:
        if await self._confirm("Close ALL positions (reduceOnly)?"):
            self.close_all_positions()
            self.refresh_all()

    async def _flow_apply_preset(self) -> None:
        if await self._confirm("Apply preset change to config?"):
            self.apply_preset()
            self.refresh_all()

    async def _flow_mode(self) -> None:
        target = "live" if self.cfg.mode == "paper" else "paper"
        msg = (
            "Switch to LIVE trading with real money?"
            if target == "live"
            else f"Switch mode to {target}?"
        )
        if await self._confirm(msg):
            self.cfg.mode = target  # type: ignore[assignment]
            self._save()
            self._fill_selects()
            self.refresh_all()

    def _flow_enable(self, bid: str) -> None:
        s = self._selected_symbol("ctl-symbol")
        if s:
            s.enabled = bid == "ctl-enable"
            self._save()
            self.refresh_all()

    async def _flow_kill(self) -> None:
        if await self._confirm("Engage KILL switch? Blocks entries, closes live all."):
            self.kill_engage("dashboard")
            self.refresh_all()

    def _flow_unblock(self) -> None:
        self.kill_reset()
        self.refresh_all()

    def _flow_tg(self) -> None:
        from . import telegram as TG

        ok = TG.send("GG-Bot Telegram-Test OK (dashboard)")
        self.query_one("#ctl-msg", Static).update(
            "sent" if ok else "not-configured-or-failed"
        )

    def _flow_backtest(self) -> None:
        self.run_worker(self._run_backtest(), exclusive=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "pos-refresh":
            self.refresh_positions()
        elif bid == "pos-closeall":
            await self._flow_close_all()
        elif bid == "ps-list":
            self.list_presets()
        elif bid == "ps-preview":
            self.preview_preset()
        elif bid == "ps-apply":
            await self._flow_apply_preset()
        elif bid == "ctl-mode":
            await self._flow_mode()
        elif bid in ("ctl-enable", "ctl-disable"):
            self._flow_enable(bid)
        elif bid == "ctl-kill":
            await self._flow_kill()
        elif bid == "ctl-unblock":
            self._flow_unblock()
        elif bid == "ctl-tg":
            self._flow_tg()
        elif bid == "bt-run":
            self._flow_backtest()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ps-filter":
            self.list_presets()
        elif event.input.id == "bt-limit":
            self.run_worker(self._run_backtest(), exclusive=True)

    def list_presets(self) -> None:
        from .presets import list_presets

        f = self.query_one("#ps-filter", Input).value.strip().lower()
        t = self.query_one("#ps-table", DataTable)
        t.clear(columns=True)
        t.add_column("preset")
        for k in list_presets():
            if not f or f in k.lower():
                t.add_row(k)

    def preview_preset(self) -> None:
        from .presets import get_preset

        asset = self.query_one("#ps-asset", Select).value
        tf = self.query_one("#ps-tf", Select).value
        t = self.query_one("#ps-preview", DataTable)
        t.clear(columns=True)
        t.add_column("key")
        t.add_column("value")
        if asset == Select.BLANK or not asset:
            t.add_row("error", "pick an asset first")
            return
        ft = self.query_one("#ps-finetune", Checkbox).value
        try:
            p = get_preset(str(asset), str(tf), bool(ft))
        except KeyError as e:
            t.add_row("error", str(e))
            return
        for k in [
            "basis",
            "style",
            "bb_len",
            "filter_mode",
            "stop_mode",
            "stop_pct",
            "tp_count",
            "tp_basis",
            "be_mode",
            "trail_mode",
        ]:
            t.add_row(k, str(p.get(k)))

    def apply_preset(self) -> None:
        s = self._selected_symbol("ps-symbol")
        asset = self.query_one("#ps-asset", Select).value
        tf = self.query_one("#ps-tf", Select).value
        if not s or asset == Select.BLANK or not asset:
            return
        s.asset_preset = str(asset)
        s.timeframe = str(tf)  # type: ignore[assignment]
        from .config import save_config
        from .state import connect, audit

        save_config(self.cfg, self._config_path)
        self.reload_config()
        db = connect(self.cfg.state_path)
        try:
            audit(
                db,
                "dashboard",
                "preset-set",
                f"{s.exchange} {s.symbol} -> {asset}_{tf}",
            )
        finally:
            db.close()

    def kill_engage(self, reason: str) -> None:
        from .state import connect, audit, engage_kill_flag

        self._risk.engage(reason)
        db = connect(self.cfg.state_path)
        try:
            import datetime

            db.execute(
                "INSERT INTO kills VALUES (?,?)",
                (datetime.datetime.now(datetime.timezone.utc).isoformat(), reason),
            )
            db.commit()
            audit(db, "dashboard", "kill-engage", reason)
        finally:
            db.close()
        engage_kill_flag(self.cfg.state_path, reason)

    def kill_reset(self) -> None:
        from .state import connect, audit, clear_kill_flag

        self._risk.reset()
        clear_kill_flag(self.cfg.state_path)
        db = connect(self.cfg.state_path)
        try:
            audit(db, "dashboard", "kill-reset", "")
        finally:
            db.close()

    def close_all_positions(self) -> None:
        from . import exchanges as X
        from .orders import close_all
        from .engine import finish_close, get_equity, last_price

        ex_cache: dict = {}

        def _ex(name: str, auth: bool):
            if name not in ex_cache:
                ex_cache[name] = X.build(name, need_auth=auth)
            return ex_cache[name]

        for s in self.cfg.symbols:
            if not s.enabled:
                continue
            if self.cfg.mode == "live":
                with suppress(Exception):
                    close_all(_ex(s.exchange, True), s.symbol)
            from .state import connect, load_open

            db = connect(self.cfg.state_path)
            try:
                oc = load_open(db, s.exchange, s.symbol)
                if oc:
                    try:
                        px = last_price(
                            _ex(s.exchange, self.cfg.mode == "live"),
                            s.symbol,
                            oc["entry"],
                        )
                    except Exception:
                        px = oc["entry"]
                    try:
                        eq = get_equity(
                            _ex(s.exchange, self.cfg.mode == "live"),
                            s.symbol,
                            self.cfg.mode,
                        )
                    except Exception:
                        eq = 10000.0
                    risk = self._risk
                    if risk is None:
                        from .risk import RiskState

                        risk = RiskState()
                        self._risk = risk
                    finish_close(
                        db,
                        risk,
                        self.cfg,
                        s,
                        oc,
                        oc.get("realized", 0.0),
                        px,
                        "DASHBOARD",
                        eq,
                    )
            finally:
                db.close()

    async def _run_backtest(self) -> None:
        out = self.query_one("#bt-result", Static)
        out.update("running...")
        s = self._selected_symbol("bt-symbol")
        try:
            limit = max(50, int(self.query_one("#bt-limit", Input).value or "300"))
        except ValueError:
            limit = 300
        if not s:
            out.update("pick a symbol first")
            return
        res = await asyncio.to_thread(self._blocking_backtest, s, limit)
        if "error" in res:
            out.update("error: " + str(res["error"])[:300])
        else:
            out.update(
                f"symbol={res['symbol']} trades={res['trades']} "
                f"winrate={res['winrate']:.1f}% total={res['total_pct']:.2f}%"
            )

    def _blocking_backtest(self, s, limit: int) -> dict:
        try:
            import pandas as pd
            from .presets import get_preset
            from . import exchanges as X
            from .backtest import run as bt

            ex = X.build(s.exchange, need_auth=False)
            ohlcv = ex.fetch_ohlcv(s.symbol, s.timeframe, limit=limit)
            df = pd.DataFrame(
                ohlcv, columns=["ts", "open", "high", "low", "close", "volume"]
            )
            preset = get_preset(s.asset_preset, s.timeframe, self.cfg.fine_tune)
            return {"symbol": s.symbol, **bt(df, preset, self.cfg.manual)}
        except Exception as e:
            return {"error": str(e)}
