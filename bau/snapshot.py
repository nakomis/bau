from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from bau.policy import elapsed_fraction
from bau.telemetry import RateLimit


@dataclass(frozen=True)
class OpenPrRow:
    repo: str
    ref: str
    title: str
    url: str
    opened: str
    age_days: int
    ci: str
    draft: bool
    unresolved_comments: int
    plane_url: str


@dataclass(frozen=True)
class DeployRow:
    repo: str
    ref: str
    title: str
    pr_url: str
    merged_at: str
    action: str


@dataclass(frozen=True)
class LastRun:
    started: str
    ended: str
    sessions: int
    prs_opened: int
    feedback_handled: int
    stop_reason: str
    telemetry: str


def build_snapshot(
    *,
    paused: bool,
    last_run: LastRun | None,
    latest: RateLimit | None,
    open_prs: list[OpenPrRow],
    deploy_pending: list[DeployRow],
    declined: list[dict],
    now: float,
    pace_override: dict | None = None,
) -> dict:
    if latest is None:
        pace = pace_override
    elif latest.five_hour and latest.seven_day:
        pace = {
            "five_hour_used": latest.five_hour.utilization,
            "five_hour_resets": latest.five_hour.resets_at,
            "seven_day_used": latest.seven_day.utilization,
            "seven_day_resets": latest.seven_day.resets_at,
            "elapsed_pct": round(elapsed_fraction(latest.seven_day.resets_at, now), 3),
        }
    else:
        pace = None
    return {
        "schema": 1,
        "generated_at": datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
        "paused": paused,
        "last_run": asdict(last_run) if last_run else None,
        "pace": pace,
        "open_prs": [asdict(r) for r in open_prs],
        "deploy_pending": [asdict(r) for r in deploy_pending],
        "declined": declined,
    }


def write_snapshot(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_snapshot(path: Path) -> dict | None:
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text())
