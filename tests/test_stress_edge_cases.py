import unittest
from datetime import datetime, timezone
from gg_bot import strategy as S
from gg_bot.engine import monitor, build_cycle, meets_min_notional

class BotEdgeCaseStressTest(unittest.TestCase):
    def setUp(self):
        self.preset = {
            "tp_count": 4, "tp_basis": "Prozent",
            "tp1": 1.0, "tp2": 2.0, "tp3": 3.0, "tp4": 5.0,
            "stop_mode": "Prozent", "stop_pct": 2.0,
            "be_mode": "TP1", "be_buffer": 0.1,
            "trail_mode": "Prozent", "trail_atr": 2.0, "trail_pct": 1.0,
            "opposite_mode": "Schliessen und drehen",
            "fee_pct": 0.05
        }
        self.cycle = build_cycle(
            cycle_no=1, side="long", qty=1.0, entry=100.0, stop=98.0,
            preset=self.preset, manual={}, atr_v=1.5
        )

    def test_flash_crash_gap_down(self):
        # Market opens at 95.0, well below stop 98.0
        ev = monitor(
            self.cycle, price=95.0, hi=96.0, lo=94.0, atr_v=1.5,
            trend_line=97.0, sig={"long": False, "short": True}, manual={}
        )
        self.assertTrue(ev["closed_all"])
        self.assertEqual(ev["why"], "SL")
        self.assertEqual(len(ev["closes"]), 1)
        self.assertEqual(ev["closes"][0][0], 1.0) # all qty closed

    def test_multi_tp_gap_up(self):
        # Huge spike to 106.0 hitting all 4 TPs (101, 102, 103, 105)
        ev = monitor(
            self.cycle, price=106.0, hi=106.0, lo=100.0, atr_v=1.5,
            trend_line=99.0, sig={"long": True, "short": False}, manual={}
        )
        # All 4 TPs hit
        self.assertEqual(len(ev["closes"]), 4)
        total_closed = sum(c[0] for c in ev["closes"])
        self.assertAlmostEqual(total_closed, 1.0, places=6)
        self.assertTrue(all(c[2] == "TP" for c in ev["closes"]))
        # Stop should have been moved to Trailing or BE
        self.assertGreater(self.cycle["stop"], 100.0)

    def test_wick_touches_both_tp_and_sl(self):
        # Conservative design: Stop-Loss is checked first!
        ev = monitor(
            self.cycle, price=99.0, hi=104.0, lo=97.5, atr_v=1.5,
            trend_line=98.0, sig={"long": False, "short": False}, manual={}
        )
        self.assertTrue(ev["closed_all"])
        self.assertEqual(ev["why"], "SL")

    def test_overnight_session_spanning_midnight(self):
        # Session from 22:00 to 04:00 UTC
        manual = {"session_on": True, "trade_session": "2200-0400", "session_days": [1,2,3,4,5,6,7]}
        # 23:30 UTC -> inside session
        t1 = datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)
        self.assertTrue(S.session_allows(t1, manual))
        # 02:15 UTC -> inside session
        t2 = datetime(2026, 10, 25, 2, 15, tzinfo=timezone.utc)
        self.assertTrue(S.session_allows(t2, manual))
        # 12:00 UTC -> outside session
        t3 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(S.session_allows(t3, manual))

    def test_min_notional_binance_edge_case(self):
        class MockBinance:
            def load_markets(self):
                pass
            def market(self, sym):
                return {"limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}}}
        ex = MockBinance()
        # 0.0001 BTC @ $65,000 = $6.50 cost (>5), but amount 0.0001 < 0.001 min_amount -> FAIL
        self.assertFalse(meets_min_notional(ex, "BTC/USDT:USDT", 0.0001, 65000.0, live=True))
        # 0.001 BTC @ $65,000 = $65.00 cost (>5), amount 0.001 >= 0.001 -> PASS
        self.assertTrue(meets_min_notional(ex, "BTC/USDT:USDT", 0.001, 65000.0, live=True))
        # 10 DOGE @ $0.15 = $1.50 cost (<5 min_cost) -> FAIL
        self.assertFalse(meets_min_notional(ex, "DOGE/USDT:USDT", 10.0, 0.15, live=True))
        # 100 DOGE @ $0.15 = $15.00 cost (>5) -> PASS
        self.assertTrue(meets_min_notional(ex, "DOGE/USDT:USDT", 100.0, 0.15, live=True))

if __name__ == "__main__":
    unittest.main()
