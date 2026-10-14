import os
import stat
from pathlib import Path

import pytest

from bau.config import load_config
from bau.session import SessionRunner, docker_command

FAKES = Path(__file__).parent / "fakes"


@pytest.fixture
def cfg(tmp_path: Path):
    from tests.test_config import MINIMAL

    p = tmp_path / "bau.yaml"
    p.write_text(MINIMAL)
    return load_config(p)


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = bindir / "docker"
    target.write_text((FAKES / "fake_docker.py").read_text())
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_BRIEF_LOG", str(tmp_path / "brief.txt"))
    monkeypatch.setenv("FAKE_KILL_LOG", str(tmp_path / "kills.txt"))
    return tmp_path


def run(cfg, tmp_path, mode: str, wall_clock: float = 60.0):
    os.environ["FAKE_MODE"] = mode
    runner = SessionRunner(cfg)
    runner.wall_clock_seconds = wall_clock
    return runner.run(
        name="bau-home-42",
        checkout=tmp_path / "wt",
        bare=tmp_path / "bare.git",
        brief="hello brief",
        model="claude-opus-5-5",
        github_token="ghs",
        oauth_token="oat",
        log_path=tmp_path / "session.log",
    )


def test_docker_command_shape(cfg, tmp_path):
    cmd = docker_command(
        cfg,
        name="bau-x",
        checkout=Path("/Users/nakomis/bau/work/x"),
        bare=Path("/Users/nakomis/bau/repos/y.git"),
        model="claude-opus-5-5",
        budget=5.0,
    )
    joined = " ".join(cmd)
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--network bau-net" in joined
    assert "-v /Users/nakomis/bau/repos/y.git:/Users/nakomis/bau/repos/y.git:ro" in joined
    assert "-v /Users/nakomis/bau/work/x:/Users/nakomis/bau/work/x" in joined
    assert "-w /Users/nakomis/bau/work/x" in joined
    assert "--max-budget-usd 5.0" in joined and "--model claude-opus-5-5" in joined
    assert "--output-format stream-json" in joined and "--dangerously-skip-permissions" in joined
    assert "ghs" not in joined and "oat" not in joined
    no_repo = docker_command(cfg, name="bau-x", checkout=Path("/w"), bare=None, model="m", budget=1.0)
    assert ":ro" not in " ".join(no_repo)


def test_ok_session_reads_brief_and_telemetry(cfg, fake_docker):
    r = run(cfg, fake_docker, "ok")
    assert r.outcome == "done" and r.cost_usd == 0.42
    assert r.latest.five_hour.utilization == 0.2
    assert (fake_docker / "brief.txt").read_text() == "hello brief"
    assert "rate_limit_event" in (fake_docker / "session.log").read_text()


def test_too_big(cfg, fake_docker):
    r = run(cfg, fake_docker, "too_big")
    assert r.outcome == "too_big" and r.reason == "schema change"


def test_rate_limited(cfg, fake_docker):
    r = run(cfg, fake_docker, "limit")
    assert r.outcome == "rate_limited" and r.latest.status == "rejected"


def test_five_hour_kill(cfg, fake_docker):
    r = run(cfg, fake_docker, "five_hour")
    assert r.outcome == "killed_five_hour"
    assert (fake_docker / "kills.txt").read_text() == "killed\n"


def test_wall_clock_kill(cfg, fake_docker):
    r = run(cfg, fake_docker, "slow", wall_clock=1.0)
    assert r.outcome == "killed_wall_clock"
    assert (fake_docker / "kills.txt").read_text() == "killed\n"


def test_crash_is_failed(cfg, fake_docker):
    r = run(cfg, fake_docker, "crash")
    assert r.outcome == "failed" and "exit 3" in r.reason
