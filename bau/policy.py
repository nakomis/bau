from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from bau.config import Limits, Schedule
from bau.telemetry import RateLimit, Window

WEEK_SECONDS = 7 * 86400


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


OK = Decision(True, "ok")


def elapsed_fraction(resets_at: int, now: float, window_seconds: int = WEEK_SECONDS) -> float:
    remaining = (resets_at - now) / window_seconds
    return min(1.0, max(0.0, 1.0 - remaining))


def pace_allows(seven_day: Window | None, now: float, margin: float) -> Decision:
    if seven_day is None:
        return Decision(False, "telemetry-missing")
    if seven_day.utilization < elapsed_fraction(seven_day.resets_at, now) - margin:
        return OK
    return Decision(False, "pace")


class Policy:
    def __init__(self, limits: Limits, schedule: Schedule, clock: Callable[[], datetime]):
        self.limits = limits
        self.schedule = schedule
        self.clock = clock

    def can_start_session(
        self,
        *,
        latest: RateLimit | None,
        open_prs: int,
        sessions_started: int,
        paused: bool,
        new_ticket: bool,
    ) -> Decision:
        if paused:
            return Decision(False, "paused")
        if latest is not None and latest.status == "rejected":
            return Decision(False, "rejected")
        now = self.clock()
        if now.timetz().replace(tzinfo=None) >= self.schedule.no_new_sessions_after:
            return Decision(False, "cutoff")
        if latest is None or latest.seven_day is None or latest.five_hour is None:
            if sessions_started < self.limits.fail_closed_sessions:
                return Decision(True, "telemetry-missing")
            return Decision(False, "telemetry-missing")
        if latest.five_hour.utilization >= self.limits.five_hour_kill:
            return Decision(False, "five-hour")
        pace = pace_allows(latest.seven_day, now.timestamp(), self.limits.pace_margin)
        if not pace.allowed:
            return pace
        if new_ticket and open_prs >= self.limits.open_pr_ceiling:
            return Decision(False, "ceiling")
        return OK
