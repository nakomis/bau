from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from bau.config import Nudge
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
class QuestionRow:
    repo: str
    ref: str
    title: str
    plane_url: str
    repo_url: str
    asked_at: str
    age_days: int
    round: int
    summary: str
    questions: list[dict]


@dataclass(frozen=True)
class LastRun:
    started: str
    ended: str
    sessions: int
    prs_opened: int
    feedback_handled: int
    stop_reason: str
    telemetry: str
    swept: int = 0
    labelled_bau: int = 0
    labelled_not_bau: int = 0
    triaged: int = 0
    asked: int = 0


def _level(count: int, oldest: int, warn_count: int, warn_days: int, down_count: int, down_days: int) -> str:
    if count >= down_count or oldest > down_days:
        return "down"
    if count >= warn_count or oldest > warn_days:
        return "warn"
    return "ok"


def nudge(open_prs: list[OpenPrRow], questions: list[QuestionRow], t: Nudge) -> dict:
    prs = _level(
        len(open_prs),
        max((r.age_days for r in open_prs), default=0),
        t.prs_warn_count,
        t.prs_warn_days,
        t.prs_down_count,
        t.prs_down_days,
    )
    qs = _level(
        len(questions),
        max((r.age_days for r in questions), default=0),
        t.questions_warn_count,
        t.questions_warn_days,
        t.questions_down_count,
        t.questions_down_days,
    )
    worst = max((prs, qs), key=("ok", "warn", "down").index)
    return {"level": worst, "prs": prs, "questions": qs}


def build_snapshot(
    *,
    paused: bool,
    last_run: LastRun | None,
    latest: RateLimit | None,
    open_prs: list[OpenPrRow],
    deploy_pending: list[DeployRow],
    declined: list[dict],
    questions: list[QuestionRow],
    nudge_thresholds: Nudge,
    uninstalled: list[str],
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
        "schema": 2,
        "generated_at": datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
        "paused": paused,
        "last_run": asdict(last_run) if last_run else None,
        "pace": pace,
        "open_prs": [asdict(r) for r in open_prs],
        "deploy_pending": [asdict(r) for r in deploy_pending],
        "declined": declined,
        "questions": [asdict(r) for r in questions],
        "nudge": nudge(open_prs, questions, nudge_thresholds),
        "uninstalled": uninstalled,
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
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def drop_open_pr(path: Path, ref: str) -> None:
    """Remove an abandoned item's PR rows from the snapshot; a missing or corrupt snapshot is left alone."""
    snap = read_snapshot(path)
    if not isinstance(snap, dict) or not isinstance(snap.get("open_prs"), list):
        return
    kept = [r for r in snap["open_prs"] if not (isinstance(r, dict) and r.get("ref") == ref)]
    if len(kept) != len(snap["open_prs"]):
        write_snapshot(path, {**snap, "open_prs": kept})


def drop_question(path: Path, ref: str) -> None:
    """Remove an answered or not-bau item's question row; a missing or corrupt snapshot is left alone."""
    snap = read_snapshot(path)
    if not isinstance(snap, dict) or not isinstance(snap.get("questions"), list):
        return
    kept = [r for r in snap["questions"] if not (isinstance(r, dict) and r.get("ref") == ref)]
    if len(kept) != len(snap["questions"]):
        write_snapshot(path, {**snap, "questions": kept})
