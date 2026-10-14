import json
import os
import stat
import time
from pathlib import Path

import pytest

from bau import session
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
    # Setting FAKE_MODE here lets monkeypatch restore it at teardown; run() overwrites it per test.
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setenv("FAKE_BRIEF_LOG", str(tmp_path / "brief.txt"))
    monkeypatch.setenv("FAKE_KILL_LOG", str(tmp_path / "kills.txt"))
    monkeypatch.setenv("FAKE_ARGV_LOG", str(tmp_path / "argv.txt"))
    monkeypatch.setenv("FAKE_ENV_LOG", str(tmp_path / "env.txt"))
    return tmp_path


@pytest.fixture
def env_files(monkeypatch):
    """Record every env file the runner creates, so tests can check it is gone afterwards."""
    seen: list[str] = []
    real_mkstemp = session.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        seen.append(path)
        return fd, path

    monkeypatch.setattr(session.tempfile, "mkstemp", spy)
    return seen


def run(cfg, tmp_path, mode: str, wall_clock: float = 60.0, **overrides):
    os.environ["FAKE_MODE"] = mode
    runner = SessionRunner(cfg)
    runner.wall_clock_seconds = wall_clock
    kwargs = dict(
        name="bau-home-42",
        checkout=tmp_path / "wt",
        bare=tmp_path / "bare.git",
        brief="hello brief",
        model="claude-opus-5-5",
        github_token="ghs",
        oauth_token="oat",
        log_path=tmp_path / "session.log",
    )
    kwargs.update(overrides)
    return runner.run(**kwargs)


def kills(tmp_path: Path) -> str:
    return (tmp_path / "kills.txt").read_text()


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
    assert "-e NODE_USE_ENV_PROXY=1" in joined
    assert "--label bau.session=1" in joined
    assert "--memory 2500m" in joined and "--pids-limit 1024" in joined
    assert "--cap-drop ALL" in joined and "--security-opt no-new-privileges" in joined
    assert cmd.index("--cap-drop") < cmd.index(cfg.container.image)
    no_repo = docker_command(cfg, name="bau-x", checkout=Path("/w"), bare=None, model="m", budget=1.0)
    assert ":ro" not in " ".join(no_repo)
    custom = docker_command(
        cfg, name="bau-x", checkout=Path("/w"), bare=None, model="m", budget=1.0, docker="/opt/bin/docker"
    )
    assert custom[0] == "/opt/bin/docker"


def test_ok_session_reads_brief_and_telemetry(cfg, fake_docker):
    r = run(cfg, fake_docker, "ok")
    assert r.outcome == "done" and r.cost_usd == 0.42
    assert r.latest.five_hour.utilization == 0.2
    assert (fake_docker / "brief.txt").read_text() == "hello brief"
    assert "rate_limit_event" in (fake_docker / "session.log").read_text()


def test_secrets_stay_off_argv_and_env_file_is_private(cfg, fake_docker, env_files):
    run(cfg, fake_docker, "ok")
    argv = json.loads((fake_docker / "argv.txt").read_text().splitlines()[0])
    assert "--env-file" in argv
    assert not any("ghs" in a or "oat" in a for a in argv)
    assert argv[argv.index("--env-file") + 1] == env_files[0]
    env = json.loads((fake_docker / "env.txt").read_text().splitlines()[0])
    assert "GH_TOKEN=ghs\n" in env["contents"]
    assert "CLAUDE_CODE_OAUTH_TOKEN=oat\n" in env["contents"]
    assert env["mode"] == "0o600"


@pytest.mark.parametrize("mode,wall_clock", [("ok", 60.0), ("slow", 1.0), ("crash", 60.0)])
def test_env_file_removed_after_run(cfg, fake_docker, env_files, mode, wall_clock):
    run(cfg, fake_docker, mode, wall_clock=wall_clock)
    assert env_files and not os.path.exists(env_files[0])


def test_newline_in_token_is_rejected_before_writing_env_file(cfg, fake_docker, env_files):
    with pytest.raises(ValueError):
        run(cfg, fake_docker, "ok", github_token="ghs\nEVIL=1")
    assert env_files == []


def test_too_big(cfg, fake_docker):
    r = run(cfg, fake_docker, "too_big")
    assert r.outcome == "too_big" and r.reason == "schema change"


def test_rate_limited(cfg, fake_docker):
    r = run(cfg, fake_docker, "limit")
    assert r.outcome == "rate_limited" and r.latest.status == "rejected"
    assert kills(fake_docker) == "killed\n"


def test_limit_text_without_rejected_event_is_rate_limited(cfg, fake_docker):
    r = run(cfg, fake_docker, "limit_text")
    assert r.outcome == "rate_limited" and r.latest is None


def test_five_hour_kill(cfg, fake_docker):
    r = run(cfg, fake_docker, "five_hour")
    assert r.outcome == "killed_five_hour"
    assert kills(fake_docker) == "killed\n"


def test_wall_clock_kill(cfg, fake_docker):
    r = run(cfg, fake_docker, "slow", wall_clock=1.0)
    assert r.outcome == "killed_wall_clock"
    assert kills(fake_docker) == "killed\n"


def test_unread_brief_cannot_hang_past_wall_clock(cfg, fake_docker):
    start = time.monotonic()
    r = run(cfg, fake_docker, "noread", wall_clock=1.0, brief="x" * 200_000)
    assert r.outcome == "killed_wall_clock"
    assert time.monotonic() - start < 4


def test_crash_is_failed(cfg, fake_docker):
    r = run(cfg, fake_docker, "crash")
    assert r.outcome == "failed" and "exit 3" in r.reason


def test_no_result_line_exit0_is_failed(cfg, fake_docker):
    r = run(cfg, fake_docker, "no_result_exit0")
    assert r.outcome == "failed" and "no result line, exit 0" in r.reason


def test_is_error_result_is_failed(cfg, fake_docker):
    r = run(cfg, fake_docker, "is_error")
    assert r.outcome == "failed" and "boom" in r.reason


def test_nonzero_exit_after_result_is_failed(cfg, fake_docker):
    r = run(cfg, fake_docker, "result_exit2")
    assert r.outcome == "failed" and "exit 2" in r.reason


def test_log_failure_kills_container_and_propagates(cfg, fake_docker, env_files):
    log_dir = fake_docker / "logdir"
    log_dir.mkdir()
    with pytest.raises(IsADirectoryError):
        run(cfg, fake_docker, "slow", log_path=log_dir)
    assert kills(fake_docker) == "killed\n"
    assert not os.path.exists(env_files[0])


def test_parser_exception_kills_container_and_propagates(cfg, fake_docker, env_files, monkeypatch):
    def boom(line):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(session, "parse_stream_line", boom)
    with pytest.raises(RuntimeError, match="parser exploded"):
        run(cfg, fake_docker, "slow")
    assert kills(fake_docker) == "killed\n"
    assert not os.path.exists(env_files[0])
