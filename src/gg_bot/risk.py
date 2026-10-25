"""Live guardrails: daily loss, kill switch, cooldown."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RiskState:
    day: str = ""
    day_pnl: float = 0.0
    killed: bool = False
    kill_reason: str = ""
    last_flat_bar: dict[str, float] = field(default_factory=dict)

    def new_day(self, today: str) -> None:
        if self.day != today:
            self.day, self.day_pnl = today, 0.0

    def register_pnl(self, pnl: float, max_daily_loss_pct: float, equity: float) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.new_day(today)
        self.day_pnl += pnl
        if equity > 0 and (-self.day_pnl) >= equity * max_daily_loss_pct / 100.0:
            self.killed, self.kill_reason = (
                True,
                f"max daily loss hit: {self.day_pnl:.2f}",
            )
            return self.kill_reason
        return ""

    def engage(self, reason: str) -> None:
        self.killed, self.kill_reason = True, reason

    def reset(self) -> None:
        self.killed, self.kill_reason = False, ""
