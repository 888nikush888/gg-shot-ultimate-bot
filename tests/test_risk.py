from gg_bot.risk import RiskState


def test_daily_loss_kill():
    r = RiskState()
    assert r.register_pnl(-600, 5.0, 10000) != ""
    assert r.killed


def test_reset():
    r = RiskState(killed=True, kill_reason="x")
    r.reset()
    assert not r.killed
