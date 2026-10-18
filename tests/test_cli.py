from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from bau.atc import AtcState, AtcUnavailable
from bau.cli import ClientFactory, build_parser, exit_code, snapshot_rows
from bau.config import Nudge
from bau.github import Token
from bau.snapshot import OpenPrRow


def test_parser_subcommands():
    p = build_parser()
    assert p.parse_args(["run", "--dry-run"]).dry_run is True
    assert p.parse_args(["snapshot"]).command == "snapshot"
    assert p.parse_args(["probe"]).command == "probe"


def test_config_dir_option_before_or_after_subcommand():
    p = build_parser()
    assert p.parse_args(["--config", "/a", "run"]).config == Path("/a")
    assert p.parse_args(["run", "--config", "/b"]).config == Path("/b")
    assert p.parse_args(["run"]).config == Path("/Users/nakomis/bau/src/config")


@pytest.mark.parametrize("reason", ["ok", "cutoff", "pace", "paused", "five-hour"])
def test_normal_stop_reasons_exit_zero(reason):
    assert exit_code(reason) == 0


@pytest.mark.parametrize("reason", ["rejected", "telemetry-missing", "something-new"])
def test_abnormal_stop_reasons_exit_one(reason):
    assert exit_code(reason) == 1


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
NUDGE = Nudge(5, 5, 10, 14, 5, 3, 10, 7)
FRESH = AtcState({}, {}, False)


class FakePr:
    def __init__(self, number, title, body="", created="2026-10-12T12:00:00Z"):
        self.number, self.title, self.body, self.created_at = number, title, body, created
        self.url = f"https://github.com/x/y/pull/{number}"
        self.head_sha, self.draft = "abc", False


class FakeGh:
    def __init__(self, prs, fail=False):
        self.prs, self.fail = prs, fail

    def open_prs(self, repo, author):
        if self.fail:
            raise RuntimeError("boom")
        return self.prs

    def review_threads(self, repo, number):
        return [SimpleNamespace(resolved=False), SimpleNamespace(resolved=True)]

    def ci_status(self, repo, sha):
        return "green"


class FakePlane:
    def project_url(self, ref):
        return f"https://plane/{ref}"

    def list_items(self, ident, label):
        return [SimpleNamespace(state="Ready for test", ref=f"{ident}-9", name="Deploy me")]


def test_snapshot_rows_survives_one_repo_failing():
    from bau.config import ProjectConfig

    projects = {
        "BAU": ProjectConfig("BAU", "a/bad", False),
        "HOME": ProjectConfig("HOME", "a/good", True, "run it"),
    }
    ghs = {
        "a/bad": FakeGh([], fail=True),
        "a/good": FakeGh([FakePr(1, "feat: x (HOME-3)"), FakePr(2, "no ref")]),
    }
    cfg = SimpleNamespace(
        identities=SimpleNamespace(worker_github_login="bot", worker_plane_email="bau@nakomis.com")
    )
    logs = []
    open_rows, deploy_rows, _ = snapshot_rows(
        cfg, projects, FakePlane(), lambda repo: ghs[repo], lambda: NOW, log=logs.append
    )
    assert [r.ref for r in open_rows] == ["HOME-3"]
    row = open_rows[0]
    assert isinstance(row, OpenPrRow)
    assert (row.age_days, row.ci, row.unresolved_comments, row.plane_url) == (
        2,
        "green",
        1,
        "https://plane/HOME-3",
    )
    assert [(d.ref, d.action) for d in deploy_rows] == [("HOME-9", "run it")]
    assert any("a/bad" in str(m) for m in logs)


def test_snapshot_ignores_unknown_last_run_keys(tmp_path, monkeypatch):
    import json

    from bau import cli
    from bau.snapshot import read_snapshot

    snap = tmp_path / "state.json"
    snap.write_text(
        json.dumps(
            {
                "last_run": {
                    "started": "s",
                    "ended": "e",
                    "sessions": 1,
                    "prs_opened": 0,
                    "feedback_handled": 0,
                    "stop_reason": "ok",
                    "telemetry": "ok",
                    "future_field": 7,
                }
            }
        )
    )
    cfg = SimpleNamespace(
        paused=False, paths=SimpleNamespace(snapshot=snap, pause_file=tmp_path / "paused"), nudge=NUDGE
    )
    monkeypatch.setattr(cli, "load_config", lambda p: cfg)
    monkeypatch.setattr(cli, "wire", lambda c, d: ({}, None, None, None, None, lambda: NOW, FRESH))
    assert cli.main(["snapshot"]) == 0
    assert read_snapshot(snap)["last_run"]["started"] == "s"


def test_client_factory_caches_and_refreshes_near_expiry():
    now = [NOW]
    minted = []

    class App:
        def installation_token(self, repo):
            n = len(minted)
            minted.append(repo)
            exp = now[0] + timedelta(minutes=60 if n == 0 else 3)
            return Token(f"t{n}", exp.isoformat().replace("+00:00", "Z"))

    made = []
    f = ClientFactory(App(), lambda: now[0], client_class=lambda tok: made.append(tok) or tok)
    assert f("o/r") == "t0"
    assert f("o/r") == "t0"
    assert len(minted) == 1
    now[0] += timedelta(minutes=56)  # within 5 minutes of expiry
    assert f("o/r") == "t1"
    assert f("o/r") == "t2"  # t1 expires in 3 min: already inside the margin, so re-minted
    assert len(minted) == 3


class StubNight:
    """Stands in for runloop.Night inside cli._dispatch."""

    instances: list = []
    outcome: BaseException | None = None

    def __init__(self, *args, **kwargs):
        from bau.runloop import NightReport

        self.kwargs = kwargs
        self.report = NightReport(sessions=1)
        StubNight.instances.append(self)

    def run(self):
        if StubNight.outcome is not None:
            raise StubNight.outcome
        return self.report


@pytest.fixture
def run_world(tmp_path, monkeypatch):
    from bau import cli

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "bau.yaml").write_text("paused: false\n")
    cfg = SimpleNamespace(
        paused=False,
        paths=SimpleNamespace(
            snapshot=tmp_path / "state.json", logs=tmp_path / "logs", pause_file=tmp_path / "paused"
        ),
        nudge=NUDGE,
        claude=SimpleNamespace(oauth_token=None),
    )
    StubNight.instances, StubNight.outcome = [], None
    monkeypatch.setattr(cli, "load_config", lambda p: cfg)
    monkeypatch.setattr(cli, "wire", lambda c, d: ({}, None, None, None, None, lambda: NOW, FRESH))
    monkeypatch.setattr(cli, "read_secret", lambda ref: "oat")
    monkeypatch.setattr(cli, "SessionRunner", lambda c, **kw: SimpleNamespace(cfg=c, **kw))
    monkeypatch.setattr(cli, "snapshot_rows", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(cli, "Night", StubNight)
    return SimpleNamespace(cli=cli, cfg=cfg, config_dir=config_dir, snapshot=tmp_path / "state.json")


def test_a_crashing_run_writes_a_failed_snapshot_and_exits_one(run_world):
    from bau.snapshot import read_snapshot

    StubNight.outcome = RuntimeError("x" * 300)
    assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 1
    last = read_snapshot(run_world.snapshot)["last_run"]
    assert last["stop_reason"] == ("failed: RuntimeError: " + "x" * 300)[:200]
    assert last["sessions"] == 1


def test_sigterm_becomes_systemexit_143_and_still_writes_the_snapshot(run_world, monkeypatch):
    import signal

    from bau.snapshot import read_snapshot

    seen = {}

    def run_and_capture(self):
        seen["handler"] = signal.getsignal(signal.SIGTERM)
        raise SystemExit(143)

    monkeypatch.setattr(StubNight, "run", run_and_capture)
    with pytest.raises(SystemExit) as info:
        run_world.cli.main(["run", "--config", str(run_world.config_dir)])
    assert info.value.code == 143
    with pytest.raises(SystemExit) as again:
        seen["handler"](signal.SIGTERM, None)
    assert again.value.code == 143
    assert read_snapshot(run_world.snapshot)["last_run"]["stop_reason"] == "failed: SystemExit: 143"
    assert signal.getsignal(signal.SIGTERM) is not seen["handler"]


def test_the_run_rereads_the_paused_flag(run_world):
    assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 0
    paused = StubNight.instances[0].kwargs["paused"]
    assert paused() is False
    (run_world.config_dir / "bau.yaml").write_text("paused: true\n")
    assert paused() is True


def test_the_run_reads_the_pause_file_outside_the_checkout(run_world, tmp_path):
    from bau.snapshot import read_snapshot

    run_world.cfg.paths.pause_file.write_text("upgrading Cere\n")
    assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 0
    assert StubNight.instances[0].kwargs["paused"]() is True
    assert read_snapshot(run_world.snapshot)["paused"] is True


def test_a_second_run_exits_quietly_while_one_holds_the_lock(run_world, capsys, monkeypatch):
    import fcntl

    monkeypatch.setattr(run_world.cli, "RUN_LOCK_WAIT", 0)

    lock = run_world.cfg.paths.logs.parent / "run.lock"
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 0
    assert "held the lock for an hour" in capsys.readouterr().out
    assert StubNight.instances == []
    assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 0
    assert len(StubNight.instances) == 1


def test_abandon_parses():
    p = build_parser()
    a = p.parse_args(["abandon", "HOME-42", "--note", "x"])
    assert (a.command, a.ref, a.note) == ("abandon", "HOME-42", "x")
    assert p.parse_args(["abandon", "HOME-42"]).note == ""


def test_serve_parses():
    assert build_parser().parse_args(["serve"]).port == 8800


def test_cli_abandon_invalid_ref_exits_2(capsys):
    from bau.cli import _abandon

    class P:
        def comments(self, ref):
            raise AssertionError("must not be reached")

    args = SimpleNamespace(ref="home-42", note="")
    cfg = SimpleNamespace(
        identities=SimpleNamespace(worker_github_login="w", worker_plane_email="e"),
        paths=SimpleNamespace(logs=Path("/nonexistent/logs")),
    )
    assert _abandon(args, cfg, ({}, P(), None, None, None, None, FRESH)) == 2
    assert "invalid ref" in capsys.readouterr().out


def test_run_that_cannot_get_the_lock_writes_a_locked_snapshot(run_world, monkeypatch, capsys):
    import fcntl

    from bau.snapshot import read_snapshot

    monkeypatch.setattr(run_world.cli, "RUN_LOCK_WAIT", 0)
    lock = run_world.cfg.paths.logs.parent / "run.lock"
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 0
    assert "held the lock for an hour" in capsys.readouterr().out
    assert read_snapshot(run_world.snapshot)["last_run"]["stop_reason"] == "locked"
    assert StubNight.instances == []


def test_run_waits_up_to_an_hour(run_world, monkeypatch):
    seen = []

    from contextlib import contextmanager

    @contextmanager
    def spy(path, wait_seconds=0, **kw):
        seen.append(wait_seconds)
        yield True

    monkeypatch.setattr(run_world.cli, "run_lock", spy)
    run_world.cfg.identities = SimpleNamespace()
    run_world.cli.main(["run", "--config", str(run_world.config_dir)])
    assert seen == [3600]


def test_snapshot_rows_builds_question_rows():
    from bau.config import ProjectConfig
    from bau.markers import Question, questions_comment
    from bau.plane import Comment, Item

    def item(ref, labels):
        return Item(ref, ref.lower(), "p", f"Title {ref}", "", "Todo", labels, None, "2026-10-01T00:00:00Z")

    qs = [Question("q1", "Which one?", "A", "because")]
    comments = {
        "HOME-5": [
            Comment("c1", "someone@else.com", "2026-10-01T00:00:00Z", "chatter"),
            Comment("c2", "bau@nakomis.com", "2026-10-10T12:00:00Z", questions_comment(2, "Sum", qs)),
        ],
        "HOME-6": [Comment("c3", "bau@nakomis.com", "2026-10-10T12:00:00Z", "no marker here")],
    }

    comments["NOREPO-1"] = comments["HOME-5"]

    class QPlane(FakePlane):
        def list_items(self, ident, label):
            if label != "bau-questions":
                return []
            if ident == "HOME":
                return [item("HOME-5", ["bau-questions"]), item("HOME-6", ["bau-questions"])]
            return [item("NOREPO-1", ["bau-questions"])]

        def comments(self, ref):
            return comments[ref]

    projects = {
        "HOME": ProjectConfig("HOME", "a/good", False),
        "NOREPO": ProjectConfig("NOREPO", None, False),
    }
    cfg = SimpleNamespace(
        identities=SimpleNamespace(worker_github_login="bot", worker_plane_email="bau@nakomis.com")
    )
    logs = []

    def factory(repo):
        assert repo is not None
        return FakeGh([])

    open_rows, deploy_rows, question_rows = snapshot_rows(
        cfg, projects, QPlane(), factory, lambda: NOW, log=logs.append
    )
    assert open_rows == [] and deploy_rows == []
    r = question_rows[0]
    assert (r.ref, r.repo, r.repo_url, r.title, r.age_days, r.round, r.summary) == (
        "HOME-5",
        "a/good",
        "https://github.com/a/good",
        "Title HOME-5",
        4,
        2,
        "Sum",
    )
    assert r.asked_at == "2026-10-10T12:00:00Z" and r.plane_url == "https://plane/HOME-5"
    assert r.questions == [{"id": "q1", "text": "Which one?", "recommendation": "A", "why": "because"}]
    norepo = [x for x in question_rows if x.ref == "NOREPO-1"][0]
    assert norepo.repo == "" and norepo.repo_url == ""
    assert not [r for r in question_rows if r.ref == "HOME-6"]
    assert any("HOME-6" in str(m) for m in logs)


def test_failed_run_snapshot_keeps_previous_questions_and_nudge(run_world):
    import json

    from bau.snapshot import read_snapshot

    kept = {"questions": [{"ref": "HOME-5"}], "nudge": {"level": "warn", "prs": "ok", "questions": "warn"}}
    run_world.snapshot.write_text(json.dumps(kept))
    StubNight.outcome = RuntimeError("x")
    assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 1
    snap = read_snapshot(run_world.snapshot)
    assert snap["questions"] == [{"ref": "HOME-5"}] and snap["nudge"]["level"] == "warn"
    assert snap["uninstalled"] == []


def test_hourly_snapshot_carries_uninstalled_forward(run_world, monkeypatch):
    import json

    from bau.snapshot import read_snapshot

    run_world.snapshot.write_text(json.dumps({"uninstalled": ["o/gone"]}))
    monkeypatch.setattr(run_world.cli, "snapshot_rows", lambda *a, **k: ([], [], []))
    assert run_world.cli.main(["snapshot", "--config", str(run_world.config_dir)]) == 0
    snap = read_snapshot(run_world.snapshot)
    assert snap["uninstalled"] == ["o/gone"] and snap["schema"] == 2


def test_snapshot_rows_keeps_previous_questions_when_plane_fails():
    from bau.config import ProjectConfig
    from bau.snapshot import nudge

    class DownPlane(FakePlane):
        def list_items(self, ident, label):
            raise RuntimeError("plane down")

    old = {
        "repo": "a/good", "ref": "HOME-5", "title": "T", "plane_url": "p", "repo_url": "u",
        "asked_at": "a", "age_days": 9, "round": 1, "summary": "s", "questions": [],
    }  # fmt: skip
    other = {**old, "ref": "OTHER-1"}
    projects = {"HOME": ProjectConfig("HOME", None, False)}
    cfg = SimpleNamespace(
        identities=SimpleNamespace(worker_github_login="bot", worker_plane_email="bau@nakomis.com")
    )
    logs = []
    _, _, rows = snapshot_rows(
        cfg, projects, DownPlane(), lambda r: None, lambda: NOW, log=logs.append,
        previous_questions=[old, other],
    )  # fmt: skip
    assert [r.ref for r in rows] == ["HOME-5"] and rows[0].age_days == 9
    assert nudge([], rows, NUDGE)["questions"] == "down"
    assert logs


def test_sweep_subcommand_is_gone():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sweep"])


# -- ATC (ATC-7) -------------------------------------------------------------------
def test_run_passes_atc_priority_and_staleness_to_the_night(run_world, monkeypatch):
    from bau.snapshot import read_snapshot

    stale = AtcState({}, {"HOME-1": 0}, True)
    monkeypatch.setattr(run_world.cli, "wire", lambda c, d: ({}, None, None, None, None, lambda: NOW, stale))

    def run(self):
        self.report.atc = self.kwargs["atc"]
        return self.report

    monkeypatch.setattr(StubNight, "run", run)
    assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 0
    kwargs = StubNight.instances[0].kwargs
    assert kwargs["priority"] == {"HOME-1": 0} and kwargs["atc"] == "stale"
    assert read_snapshot(run_world.snapshot)["last_run"]["atc"] == "stale"


def test_run_without_atc_or_cache_stops_preflight_atc(run_world, monkeypatch):
    from bau.snapshot import read_snapshot

    previous = {"open_prs": [{"ref": "HOME-1"}], "questions": [], "deploy_pending": []}
    run_world.snapshot.write_text(__import__("json").dumps(previous))
    run_world.cfg.schedule = SimpleNamespace(timezone="Europe/London")

    def down(c, d):
        raise AtcUnavailable("ATC GET /api/projects: 502")

    monkeypatch.setattr(run_world.cli, "wire", down)
    assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == exit_code("preflight: atc")
    snap = read_snapshot(run_world.snapshot)
    assert snap["last_run"]["stop_reason"] == "preflight: atc"
    assert snap["open_prs"] == [{"ref": "HOME-1"}]  # kept: no service was touched
    assert StubNight.instances == []


def test_dry_run_without_atc_says_so_and_writes_nothing(run_world, monkeypatch, capsys):
    def down(c, d):
        raise AtcUnavailable("no route")

    monkeypatch.setattr(run_world.cli, "wire", down)
    assert run_world.cli.main(["run", "--dry-run", "--config", str(run_world.config_dir)]) == 1
    assert "preflight: atc" in capsys.readouterr().out
    assert not run_world.snapshot.exists()


def test_locked_out_without_atc_still_records_locked(run_world, monkeypatch):
    import fcntl

    from bau.snapshot import read_snapshot

    run_world.cfg.schedule = SimpleNamespace(timezone="Europe/London")

    def down(c, d):
        raise AtcUnavailable("no route")

    monkeypatch.setattr(run_world.cli, "wire", down)
    monkeypatch.setattr(run_world.cli, "RUN_LOCK_WAIT", 0)
    lock = run_world.cfg.paths.logs.parent / "run.lock"
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 0
    assert read_snapshot(run_world.snapshot)["last_run"]["stop_reason"] == "locked"


def test_wire_reads_projects_and_priority_from_atc(tmp_path, monkeypatch, cfg):

    from bau import cli
    from tests.test_atc import P_BAU, P_HOME, fake_atc

    made = []
    real = cli.AtcClient

    def client(url, cert, key):
        made.append((url, cert, key))
        return real(url, cert, key, transport=fake_atc(projects=[P_HOME, P_BAU], refs=["BAU-2"]))

    monkeypatch.setattr(cli, "AtcClient", client)
    monkeypatch.setattr(cli, "read_secret", lambda ref: "secret")
    monkeypatch.setattr(cli, "GitHubApp", lambda *a: SimpleNamespace())
    monkeypatch.setattr(cli, "PlaneClient", lambda *a: SimpleNamespace())
    projects, plane, app, factory, repos, clock, atc = cli.wire(cfg, tmp_path)
    assert list(projects) == ["HOME", "BAU"] and atc.priority == {"BAU-2": 0} and not atc.stale
    assert made == [("https://atc.plane.home.nakomis.com", cfg.plane.client_cert, cfg.plane.client_key)]
    assert cfg.paths.atc_cache.exists()


def test_empty_atc_answer_is_recorded_as_empty(run_world, monkeypatch):
    from bau.snapshot import read_snapshot

    empty = AtcState({}, {}, False, empty=True, fetched_at="2026-10-14T11:00:00+00:00")
    monkeypatch.setattr(run_world.cli, "wire", lambda c, d: ({}, None, None, None, None, lambda: NOW, empty))

    def run(self):
        self.report.atc = self.kwargs["atc"]
        return self.report

    monkeypatch.setattr(StubNight, "run", run)
    assert run_world.cli.main(["run", "--config", str(run_world.config_dir)]) == 0
    assert StubNight.instances[0].kwargs["atc"] == "empty"
    last = read_snapshot(run_world.snapshot)["last_run"]
    assert last["atc"] == "empty" and last["atc_age_hours"] is None


def test_stale_snapshot_shows_the_cache_age(run_world, monkeypatch):
    from bau.snapshot import read_snapshot

    stale = AtcState({}, {}, True, fetched_at="2026-10-13T00:00:00+00:00")  # NOW is 14 Oct 12:00 UTC
    monkeypatch.setattr(run_world.cli, "wire", lambda c, d: ({}, None, None, None, None, lambda: NOW, stale))

    def run(self):
        self.report.atc = self.kwargs["atc"]
        return self.report

    monkeypatch.setattr(StubNight, "run", run)
    run_world.cli.main(["run", "--config", str(run_world.config_dir)])
    last = read_snapshot(run_world.snapshot)["last_run"]
    assert last["atc"] == "stale"
    assert last["atc_fetched_at"] == "2026-10-13T00:00:00+00:00" and last["atc_age_hours"] == 36.0


@pytest.mark.parametrize(
    "argv",
    [["probe"], ["snapshot"], ["abandon", "HOME-1"]],
)
def test_other_commands_without_atc_or_cache_fail_in_one_line(run_world, monkeypatch, capsys, argv):
    # Final review M5: a clear message and a non-zero exit, no traceback, and nothing touched.
    previous = '{"open_prs": [{"ref": "HOME-1"}]}'
    run_world.snapshot.write_text(previous)

    def down(c, d):
        raise AtcUnavailable("ATC GET /api/projects: ConnectError: no route")

    monkeypatch.setattr(run_world.cli, "wire", down)
    assert run_world.cli.main([*argv, "--config", str(run_world.config_dir)]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == [
        f"bau {argv[0]}: preflight: atc: ATC GET /api/projects: ConnectError: no route"
        " (and no cached copy); nothing done"
    ]
    assert run_world.snapshot.read_text() == previous


def test_serve_without_atc_or_cache_serves_with_no_projects(run_world, monkeypatch, capsys):
    served = {}

    def down(c, d):
        raise AtcUnavailable("no route")

    def services(cfg, state):
        served["state"] = state
        return state.projects, "plane", "app", "factory", "repos", lambda: NOW, state

    import bau.api

    monkeypatch.setattr(run_world.cli, "wire", down)
    monkeypatch.setattr(run_world.cli, "wire_services", services)
    monkeypatch.setattr(bau.api, "create_app", lambda cfg, projects, *a: ("app", projects))
    monkeypatch.setattr(bau.api, "serve", lambda app, port: served.setdefault("app", app))
    assert run_world.cli.main(["serve", "--config", str(run_world.config_dir)]) == 0
    assert served["app"] == ("app", {}) and served["state"].stale
    assert "serving with no projects" in capsys.readouterr().err
