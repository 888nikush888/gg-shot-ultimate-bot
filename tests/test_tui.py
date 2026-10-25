import asyncio
import os
import shutil
import tempfile
import yaml
from gg_bot.tui import hash_password, verify_password, DashboardApp

def _test_config():
    d = tempfile.TemporaryDirectory()
    src = open("config/config.example.yaml", encoding="utf-8").read()
    raw = yaml.safe_load(src)
    raw["state_path"] = d.name + "/state.db"
    raw["symbols"] = [raw["symbols"][0]]
    p = d.name + "/config.yaml"
    open(p, "w", encoding="utf-8").write(yaml.safe_dump(raw))
    return d, p

def test_password_roundtrip():
    os.environ["DASHBOARD_PASSWORD_HASH"] = hash_password("secret123")
    try:
        assert verify_password("secret123") is True
        assert verify_password("wrong") is False
    finally:
        del os.environ["DASHBOARD_PASSWORD_HASH"]
    assert verify_password("secret123") is False

def test_save_config_backup_and_validate():
    from gg_bot.config import load_config, save_config
    d, p = _test_config()
    try:
        cfg = load_config(p)
        bak = save_config(cfg, p)
        assert bak.exists() and load_config(p).mode == cfg.mode
    finally:
        shutil.rmtree(d.name, ignore_errors=True)

def _run(coro_factory=None):
    async def main():
        d, p = _test_config()
        try:
            app = DashboardApp(p)
            async with app.run_test() as pilot:
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                assert len(app.query("TabPane")) == 6
                tabs.active = "tab-positions"
                await pilot.pause()
                assert tabs.active == "tab-positions"
                tabs.active = "tab-control"
                await pilot.pause()
        finally:
            d.cleanup()
    asyncio.run(main())

def test_dashboard_boots_and_switches_tabs():
    _run()

def test_kill_engage_reset_roundtrip():
    d, p = _test_config()
    try:
        from gg_bot.state import connect, kill_flag_reason
        app = DashboardApp(p)
        app.kill_engage("test")
        assert app._risk.killed is True
        db = connect(app.cfg.state_path)
        try:
            assert db.execute("SELECT COUNT(*) FROM kills").fetchone()[0] == 1
        finally:
            db.close()
        assert kill_flag_reason(app.cfg.state_path) == "test"
        app.kill_reset()
        assert app._risk.killed is False
        assert kill_flag_reason(app.cfg.state_path) is None
    finally:
        shutil.rmtree(d.name, ignore_errors=True)
