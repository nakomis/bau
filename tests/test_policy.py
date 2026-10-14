from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from bau.config import Limits, Schedule
from bau.policy import Decision, Policy, elapsed_fraction, pace_allows
from bau.telemetry import RateLimit, Window

LIMITS = Limits(50, 0.10, 0.80, 60, 5.0, 2, 3, 15, 400, 15)
SCHEDULE = Schedule(time(5, 0), "Europe/London", time(0, 50))
LONDON = ZoneInfo("Europe/London")
WEEK = 7 * 86400


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 15, hour, minute, tzinfo=LONDON)


def policy(now: datetime) -> Policy:
    return Policy(LIMITS, SCHEDULE, clock=lambda: now)


def rl(seven: float, five: float = 0.1, status: str = "allowed", resets_in: float = WEEK / 2) -> RateLimit:
    now = at(2).timestamp()
    return RateLimit(status, Window(five, int(now + 3600)), Window(seven, int(now + resets_in)))


def test_elapsed_fraction_counts_from_the_reset_time():
    now = 1_000_000
    assert elapsed_fraction(now + WEEK, now) == 0.0
    assert elapsed_fraction(now + WEEK / 2, now) == 0.5
    assert elapsed_fraction(now, now) == 1.0
    assert elapsed_fraction(now - 10, now) == 1.0


def test_pace_allows_when_used_is_under_elapsed_minus_margin():
    now = at(2).timestamp()
    half_week = Window(0.30, int(now + WEEK / 2))  # elapsed 0.5, 0.5 - 0.1 = 0.4 > 0.30
    assert pace_allows(half_week, now, 0.10) == Decision(True, "ok")
    over = Window(0.45, int(now + WEEK / 2))
    assert pace_allows(over, now, 0.10) == Decision(False, "pace")


def test_pace_with_no_window_is_telemetry_missing():
    assert pace_allows(None, 0, 0.10) == Decision(False, "telemetry-missing")


def test_paused_beats_everything():
    d = policy(at(2)).can_start_session(
        latest=rl(0.0), open_prs=0, sessions_started=0, paused=True, new_ticket=True
    )
    assert d == Decision(False, "paused")


def test_rejected_status_stops_the_night():
    d = policy(at(2)).can_start_session(
        latest=rl(0.0, status="rejected"), open_prs=0, sessions_started=0, paused=False, new_ticket=True
    )
    assert d == Decision(False, "rejected")


def test_missing_telemetry_fails_closed_after_two_sessions():
    p = policy(at(2))
    assert p.can_start_session(
        latest=None, open_prs=0, sessions_started=1, paused=False, new_ticket=True
    ).allowed
    d = p.can_start_session(latest=None, open_prs=0, sessions_started=2, paused=False, new_ticket=True)
    assert d == Decision(False, "telemetry-missing")


def test_five_hour_kill_threshold():
    d = policy(at(2)).can_start_session(
        latest=rl(0.0, five=0.81), open_prs=0, sessions_started=0, paused=False, new_ticket=True
    )
    assert d == Decision(False, "five-hour")


def test_cutoff_applies_to_every_session_type():
    for new_ticket in (True, False):
        d = policy(at(5, 1)).can_start_session(
            latest=rl(0.0), open_prs=0, sessions_started=0, paused=False, new_ticket=new_ticket
        )
        assert d == Decision(False, "cutoff")
    assert (
        policy(at(4, 59))
        .can_start_session(latest=rl(0.0), open_prs=0, sessions_started=0, paused=False, new_ticket=False)
        .allowed
    )


def test_ceiling_applies_only_to_new_tickets():
    p = policy(at(2))
    assert p.can_start_session(
        latest=rl(0.0), open_prs=50, sessions_started=0, paused=False, new_ticket=True
    ) == Decision(False, "ceiling")
    assert p.can_start_session(
        latest=rl(0.0), open_prs=50, sessions_started=0, paused=False, new_ticket=False
    ).allowed


def test_ceiling_holds_when_telemetry_is_missing():
    d = policy(at(2)).can_start_session(
        latest=None, open_prs=50, sessions_started=0, paused=False, new_ticket=True
    )
    assert d == Decision(False, "ceiling")


def test_cutoff_uses_the_schedule_timezone_not_the_clock_offset():
    # 04:30 UTC on 15 Oct 2026 is 05:30 Europe/London (BST): past the 05:00 cutoff.
    utc_now = datetime(2026, 10, 15, 4, 30, tzinfo=ZoneInfo("UTC"))
    d = Policy(LIMITS, SCHEDULE, clock=lambda: utc_now).can_start_session(
        latest=rl(0.0), open_prs=0, sessions_started=0, paused=False, new_ticket=False
    )
    assert d == Decision(False, "cutoff")


def test_naive_clock_is_rejected():
    naive = datetime(2026, 10, 15, 2, 0)
    p = Policy(LIMITS, SCHEDULE, clock=lambda: naive)
    with pytest.raises(ValueError):
        p.can_start_session(latest=rl(0.0), open_prs=0, sessions_started=0, paused=False, new_ticket=True)
