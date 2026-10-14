import json
from pathlib import Path

import pytest

from bau.snapshot import DeployRow, LastRun, OpenPrRow, build_snapshot, read_snapshot, write_snapshot
from bau.telemetry import RateLimit, Window


def test_snapshot_shape_and_pace_fields():
    now = 1_000_000
    latest = RateLimit("allowed", Window(0.2, now + 3600), Window(0.3, now + 302400))
    snap = build_snapshot(
        paused=False,
        last_run=LastRun("2026-10-15T01:00:00Z", "2026-10-15T03:10:00Z", 4, 3, 1, "cutoff", "ok"),
        latest=latest,
        open_prs=[
            OpenPrRow(
                "nakomis/home-infra",
                "HOME-42",
                "Rotate logs",
                "https://x/pull/7",
                "2026-10-15T01:30:00Z",
                2,
                "green",
                False,
                1,
                "https://plane/HOME-42",
            )
        ],
        deploy_pending=[
            DeployRow(
                "nakomis/home-infra",
                "HOME-40",
                "Old thing",
                "https://x/pull/5",
                "2026-10-14T12:00:00Z",
                "Run the playbook",
            )
        ],
        declined=[{"ref": "HOME-99", "reason": "unmapped project", "at": "2026-10-15T01:05:00Z"}],
        now=now,
    )
    assert snap["schema"] == 1
    assert snap["pace"] == {
        "five_hour_used": 0.2,
        "five_hour_resets": now + 3600,
        "seven_day_used": 0.3,
        "seven_day_resets": now + 302400,
        "elapsed_pct": 0.5,
    }
    assert snap["open_prs"][0]["age_days"] == 2 and snap["open_prs"][0]["ref"] == "HOME-42"
    assert snap["deploy_pending"][0]["action"] == "Run the playbook"
    assert snap["last_run"]["stop_reason"] == "cutoff"


def test_snapshot_without_telemetry_has_null_pace():
    snap = build_snapshot(
        paused=True, last_run=None, latest=None, open_prs=[], deploy_pending=[], declined=[], now=5
    )
    assert snap["pace"] is None and snap["paused"] is True and snap["last_run"] is None


def test_pace_override_kept_without_telemetry():
    snap = build_snapshot(
        paused=False,
        last_run=None,
        latest=None,
        open_prs=[],
        deploy_pending=[],
        declined=[],
        now=5,
        pace_override={"x": 1},
    )
    assert snap["pace"] == {"x": 1}


def test_write_is_atomic_and_readable(tmp_path: Path):
    p = tmp_path / "state.json"
    write_snapshot(p, {"schema": 1, "x": 1})
    assert json.loads(p.read_text()) == {"schema": 1, "x": 1}
    assert read_snapshot(p) == {"schema": 1, "x": 1}
    assert read_snapshot(tmp_path / "missing.json") is None
    assert not list(tmp_path.glob("*.tmp*"))


def test_failed_write_leaves_no_temp_file(tmp_path: Path):
    with pytest.raises(TypeError):
        write_snapshot(tmp_path / "state.json", {"x": object()})
    assert list(tmp_path.iterdir()) == []


def test_pace_override_used_only_when_no_latest():
    kept = {"five_hour_used": 0.1}
    kw = dict(paused=False, last_run=None, open_prs=[], deploy_pending=[], declined=[], now=0.0)
    assert build_snapshot(latest=None, pace_override=kept, **kw)["pace"] == kept
    assert build_snapshot(latest=None, **kw)["pace"] is None
