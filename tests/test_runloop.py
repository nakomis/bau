import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bau.config import Models, ProjectConfig, load_config
from bau.github import PR, Thread, ThreadComment, Token
from bau.plane import Comment, Item
from bau.runloop import Night, branch_name, model_for, previous_state, repo_for
from bau.session import SessionResult
from bau.telemetry import RateLimit, Window
from tests.fakes.clients import FakeGitHub, FakePlane, FakeRepos, FakeRunner

LONDON = ZoneInfo("Europe/London")
REPO = "nakomis/home-infra"
PROJECTS = {"HOME": ProjectConfig("HOME", REPO, True, "Run the playbook")}


def item(ref="HOME-42", name="Rotate logs", labels=("BAU",), desc="do it", state="New"):
    return Item(ref, ref.lower(), "p1", name, desc, state, list(labels), "medium", "2026-10-01T00:00:00Z")


def pr(number=7, ref="HOME-42", sha="abc", draft=True, mergeable="clean"):
    return PR(
        REPO,
        number,
        f"PR_{number}",
        f"fix: x ({ref})",
        f"https://x/pull/{number}",
        f"{ref.lower()}-rotate-logs",
        sha,
        draft,
        "2026-10-14T01:00:00Z",
        None,
        f"Plane: {ref}",
        mergeable,
        2,
        10,
        2,
    )


def good_rl():
    now = datetime(2026, 10, 15, 2, tzinfo=LONDON).timestamp()
    return RateLimit("allowed", Window(0.1, int(now + 3600)), Window(0.1, int(now + 300000)))


@pytest.fixture
def cfg(tmp_path: Path):
    from tests.test_config import MINIMAL

    p = tmp_path / "bau.yaml"
    p.write_text(MINIMAL.replace("/Users/nakomis/bau", str(tmp_path)))
    return load_config(p)


class StubApp:
    def __init__(self):
        self.minted = 0

    def installation_token(self, repo):
        self.minted += 1
        return Token(f"ghs{self.minted}", "2026-10-15T03:00:00Z")


def night(cfg, plane, gh, runner, repos, hour=2, minute=0, dry_run=False, probe=None, app=None):
    clock = lambda: datetime(2026, 10, 15, hour, minute, tzinfo=LONDON)  # noqa: E731
    n = Night(
        cfg,
        PROJECTS,
        plane,
        app or StubApp(),
        lambda repo: gh,
        repos,
        runner,
        clock,
        dry_run=dry_run,
        oauth_token="oat",
        log=lambda *_: None,
        sleep=lambda s: None,
    )
    n.probe = lambda: probe if probe is not None else good_rl()
    return n


def opens_pr(gh, opened):
    return lambda: gh.open.__setitem__(REPO, [opened])


def test_helpers():
    assert branch_name("HOME-42", "Rotate logs: nightly!") == "home-42-rotate-logs-nightly"
    assert repo_for(item(desc="repo: nakomis/other\nrest"), PROJECTS) == "nakomis/other"
    assert repo_for(item(ref="ZZZ-1"), PROJECTS) is None
    assert model_for(item(labels=("BAU", "BAU-fable")), Models("o", "f", "BAU-fable")) == "f"
    assert (
        previous_state([Comment("c", "bau@nakomis.com", "", "[bau:picked-up from=Ready] branch x")])
        == "Ready"
    )


def test_ref_of_prefers_body_then_title():
    assert Night.ref_of(pr(ref="HOME-9")) == "HOME-9"
    bare = PR(REPO, 1, "n", "fix (BAU-3)", "u", "b", "s", True, "t", None, "no ref", "clean")
    assert Night.ref_of(bare) == "BAU-3"
    assert Night.ref_of(PR(REPO, 1, "n", "fix", "u", "b", "s", True, "t", None, "", "clean")) is None


def test_new_ticket_happy_path_opens_pr_and_marks_ready(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "green"})
    runner = FakeRunner(
        [SessionResult("done", "", good_rl(), 0.5, "Opened https://x/pull/7")], on_run=opens_pr(gh, pr())
    )
    repos = FakeRepos(tmp_path)
    report = night(cfg, plane, gh, runner, repos).run()
    assert report.sessions == 1 and report.prs_opened == 1
    assert ("create_checkout", REPO, "home-42-rotate-logs") in repos.calls
    assert ("set_state", ("HOME-42", "In progress")) in plane.calls
    assert ("assign", ("HOME-42", "bau@nakomis.com")) in plane.calls
    assert any(c[0] == "comment" and "[bau:picked-up from=New]" in c[1][1] for c in plane.calls)
    assert ("mark_ready", ("PR_7",)) in gh.calls
    assert ("link", ("HOME-42", "https://x/pull/7", "PR #7")) in plane.calls
    assert runner.briefs[0][1] == "claude-opus-5-5"


def test_red_ci_gets_one_fix_session_with_summaries_then_ready(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "red"}, summaries="tests: 3 failed\ntest_x broke")
    runs = []

    def on_run():
        runs.append(1)
        if len(runs) == 1:
            gh.open[REPO] = [pr()]
        else:
            gh.ci["abc"] = "green"

    runner = FakeRunner(
        [SessionResult("done", "", good_rl(), 0.5, "pr"), SessionResult("done", "", good_rl(), 0.2, "fixed")],
        on_run=on_run,
    )
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert report.sessions == 2 and report.prs_opened == 1
    assert "test_x broke" in runner.briefs[1][2]
    assert (("comment", (REPO, 7, "[bau:ci-fix] 1/3"))) in gh.calls
    assert ("mark_ready", ("PR_7",)) in gh.calls


def test_red_ci_that_stays_red_leaves_draft_with_comment(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "red"})
    runner = FakeRunner(
        [SessionResult("done", "", good_rl(), 0.5, "pr"), SessionResult("done", "", good_rl(), 0.2, "x")],
        on_run=opens_pr(gh, pr()),
    )
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert not any(c[0] == "mark_ready" for c in gh.calls)
    assert any(c[0] == "comment" and "draft" in c[1][1] and "red" in c[1][1] for c in plane.calls)


def test_too_big_restores_state_and_keeps_label(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(state="Ready")]})
    gh = FakeGitHub(open_prs={REPO: []})
    runner = FakeRunner(
        [SessionResult("too_big", "schema change", good_rl(), 0.1, "BAU_TOO_BIG: schema change")]
    )
    repos = FakeRepos(tmp_path)
    night(cfg, plane, gh, runner, repos).run()
    assert ("set_state", ("HOME-42", "Ready")) in plane.calls
    assert not any(c[0] == "remove_labels" for c in plane.calls)
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls


def test_previous_state_survives_a_repeat_pickup(cfg, tmp_path):
    plane = FakePlane(
        {"HOME": [item(state="In progress")]},
        comments={"HOME-42": [Comment("c", "bau@nakomis.com", "", "[bau:picked-up from=Ready] x")]},
    )
    runner = FakeRunner([SessionResult("too_big", "big", good_rl(), 0.1, "BAU_TOO_BIG: big")])
    night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, FakeRepos(tmp_path)).run()
    assert ("set_state", ("HOME-42", "Ready")) in plane.calls
    assert any(c[0] == "comment" and "[bau:picked-up from=Ready]" in c[1][1] for c in plane.calls)


def test_diff_over_threshold_is_closed_as_too_big_even_if_agent_opened_a_pr(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "green"})
    runner = FakeRunner(
        [SessionResult("done", "", good_rl(), 0.5, "Opened https://x/pull/7")], on_run=opens_pr(gh, pr())
    )
    repos = FakeRepos(tmp_path, stats=(20, 50))
    report = night(cfg, plane, gh, runner, repos).run()
    assert ("close_pr", (REPO, 7)) in gh.calls
    assert not any(c[0] == "mark_ready" for c in gh.calls)
    assert ("set_state", ("HOME-42", "New")) in plane.calls
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls
    assert report.prs_opened == 0


def test_feedback_first_runs_before_new_tickets_and_acks(cfg, tmp_path):
    existing = pr(number=7, ref="HOME-40")
    plane = FakePlane(
        {"HOME": [item(ref="HOME-40", state="In progress"), item(ref="HOME-42")]},
        comments={"HOME-40": [Comment("c1", "martin@nakomis.com", "t", "change approach")]},
    )
    gh = FakeGitHub(
        open_prs={REPO: [existing]},
        threads={7: [Thread("T1", False, [ThreadComment("nakomis", "rename", "t")])]},
        ci={"abc": "green"},
    )
    runner = FakeRunner(
        [SessionResult("done", "", good_rl(), 0.3, "fixed"), SessionResult("done", "", good_rl(), 0.3, "x")]
    )
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert runner.briefs[0][0] == "bau-home-40" and "REVIEW THREAD T1" in runner.briefs[0][2]
    assert "PLANE COMMENT c1" in runner.briefs[0][2]
    assert any(c[0] == "comment" and "[bau:ack c1]" in c[1][1] for c in plane.calls)
    assert report.feedback_handled == 1


def red_pr_world(existing_comments):
    existing = pr(number=7, ref="HOME-40")
    plane = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: [existing]}, ci={"abc": "red"}, issue_comments={7: existing_comments})
    return plane, gh


def test_ci_red_feedback_session_posts_counted_marker(cfg, tmp_path):
    plane, gh = red_pr_world(["[bau:ci-fix] 1/3"])
    gh.summaries = "boom in test_y"
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.3, "fixed")])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert "boom in test_y" in runner.briefs[0][2]
    assert ("comment", (REPO, 7, "[bau:ci-fix] 2/3")) in gh.calls


def test_ci_fix_limit_reached_leaves_draft_and_comments_once(cfg, tmp_path):
    plane, gh = red_pr_world(["[bau:ci-fix] 1/3", "[bau:ci-fix] 2/3", "[bau:ci-fix] 3/3"])
    runner = FakeRunner([])
    for _ in range(2):
        night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert runner.briefs == []
    leaving = [c for c in gh.calls if c[0] == "comment" and "leaving this for Martin" in c[1][2]]
    assert len(leaving) == 1
    assert leaving[0][1][2] == "[bau] CI still red after 3 fix sessions; leaving this for Martin."
    assert not any(c[0] == "mark_ready" for c in gh.calls)


def test_declines_unprotected_empty_and_unmapped_without_crashing(cfg, tmp_path):
    plane = FakePlane(
        {
            "HOME": [
                item(desc="repo: nakomis/nowhere"),
                item(ref="HOME-43", desc=""),
                item(ref="ZZZ-1", desc="x"),
            ]
        }
    )
    gh = FakeGitHub(open_prs={}, protected={"nakomis/nowhere": False})
    runner = FakeRunner([])
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert report.sessions == 0
    assert {d["ref"] for d in report.declined} == {"HOME-42", "HOME-43", "ZZZ-1"}
    reasons = {c[1][0]: c[1][1] for c in plane.calls if c[0] == "comment"}
    assert set(reasons) == {"HOME-42", "HOME-43", "ZZZ-1"}
    assert all(v.startswith("[bau] Not picked up:") for v in reasons.values())
    assert all(c[0] != "set_state" for c in plane.calls)


def test_decline_comment_is_not_repeated_on_later_nights(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(desc="")]})
    night(cfg, plane, FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path)).run()
    text = next(c[1][1] for c in plane.calls if c[0] == "comment")
    plane.comments_by_ref["HOME-42"] = [Comment("c", "bau@nakomis.com", "", text)]
    plane.calls.clear()
    night(cfg, plane, FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path)).run()
    assert plane.calls == []


def test_pace_fails_at_preflight(cfg, tmp_path):
    now = datetime(2026, 10, 15, 2, tzinfo=LONDON).timestamp()
    bad = RateLimit("allowed", Window(0.1, int(now + 3600)), Window(0.9, int(now + 300000)))
    plane = FakePlane({"HOME": [item()]})
    report = night(cfg, plane, FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path), probe=bad).run()
    assert report.stop_reason == "pace" and report.sessions == 0


def test_rejected_ends_the_night_at_once(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(), item(ref="HOME-43", name="Other")]})
    gh = FakeGitHub(open_prs={REPO: []})
    runner = FakeRunner([SessionResult("rate_limited", "hit your limit", None, 0.1, "hit your limit")])
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert report.sessions == 1 and report.stop_reason == "rejected"


def test_dry_run_writes_nothing(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []})
    report = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path), dry_run=True).run()
    assert report.sessions == 0 and plane.calls == [] and gh.calls == []
    assert report.planned == ["new-ticket HOME-42 -> nakomis/home-infra (claude-opus-5-5)"]


def test_merged_prs_close_the_loop(cfg, tmp_path):
    merged = PR(
        REPO,
        5,
        "PR_5",
        "fix (HOME-40)",
        "https://x/pull/5",
        "home-40-x",
        "m",
        False,
        "2026-10-13T01:00:00Z",
        "2026-10-14T12:00:00Z",
        "Plane: HOME-40",
        "clean",
    )
    plane = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={}, merged={REPO: [merged]})
    night_repos = FakeRepos(tmp_path)
    night(cfg, plane, gh, FakeRunner([]), night_repos).run()
    assert ("set_state", ("HOME-40", "Ready for test")) in plane.calls
    assert any(c == ("delete_checkout", "home-40-x") for c in night_repos.calls)


def test_merged_pr_in_non_deploying_project_is_done_and_not_handled_twice(cfg, tmp_path):
    merged = PR(
        "nakomis/bau",
        5,
        "n",
        "fix (BAU-4)",
        "u",
        "bau-4-x",
        "m",
        False,
        "t",
        "2026-10-14T12:00:00Z",
        "",
        "clean",
    )
    projects = {"BAU": ProjectConfig("BAU", "nakomis/bau", False)}
    plane = FakePlane({"BAU": [item(ref="BAU-4", state="In progress")]})
    gh = FakeGitHub(open_prs={}, merged={"nakomis/bau": [merged]})
    n = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path))
    n.projects = projects
    n.run()
    assert ("set_state", ("BAU-4", "Done")) in plane.calls
    plane.calls.clear()
    plane.items["BAU"] = [item(ref="BAU-4", state="Done")]
    n2 = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path))
    n2.projects = projects
    n2.run()
    assert plane.calls == []


def test_done_items_with_the_label_are_not_picked_up_again(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(state="Done")]})
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), FakeRunner([]), FakeRepos(tmp_path)).run()
    assert report.sessions == 0 and plane.calls == []


def test_wait_for_ci_polls_until_settled(cfg, tmp_path):
    sleeps = []
    gh = FakeGitHub(ci={"abc": ["pending", "none", "green"]})
    n = night(cfg, FakePlane({}), gh, FakeRunner([]), FakeRepos(tmp_path))
    n.sleep = sleeps.append
    assert n.wait_for_ci(gh, REPO, "abc", poll_s=30) == "green"
    assert sleeps == [30, 30]


def test_wait_for_ci_times_out_with_last_status(cfg, tmp_path):
    sleeps = []
    gh = FakeGitHub(ci={"abc": "pending"})
    n = night(cfg, FakePlane({}), gh, FakeRunner([]), FakeRepos(tmp_path))
    n.sleep = sleeps.append
    assert n.wait_for_ci(gh, REPO, "abc", timeout_s=90, poll_s=30) == "pending"
    assert sleeps == [30, 30, 30]


def test_tokens_are_reminted_when_close_to_expiry(cfg, tmp_path):
    app = StubApp()
    n = night(cfg, FakePlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path), app=app)
    assert n.token(REPO) == "ghs1" and n.token(REPO) == "ghs1"
    n.clock = lambda: datetime(2026, 10, 15, 3, 55, tzinfo=LONDON)  # 02:55 UTC, token expires 03:00
    assert n.token(REPO) == "ghs2"


def test_probe_uses_subscription_login_and_never_bare(cfg, tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bau.runloop.subprocess.run", fake_run)
    n = night(cfg, FakePlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path))
    assert Night.probe(n) is None
    assert "--disable-slash-commands" in seen["cmd"] and "--bare" not in seen["cmd"]
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oat"


def test_a_crashing_ticket_does_not_abort_the_night(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(), item(ref="HOME-43", name="Other")]})
    gh = FakeGitHub(open_prs={REPO: []})
    runner = FakeRunner([RuntimeError("docker exploded"), SessionResult("done", "", good_rl(), 0.1, "x")])
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert [b[0] for b in runner.briefs] == ["bau-home-42", "bau-home-43"]
    assert report.sessions == 2
    failed = [d for d in report.declined if d["ref"] == "HOME-42"]
    assert len(failed) == 1 and failed[0]["reason"] == "error: RuntimeError: docker exploded"
    assert any(c[0] == "comment" and "Error while working this ticket" in c[1][1] for c in plane.calls)


def test_a_crashing_pr_in_the_feedback_pass_is_recorded_and_skipped(cfg, tmp_path):
    plane = FakePlane(
        {"HOME": [item(ref="HOME-40", state="In progress"), item(ref="HOME-41", state="In progress")]}
    )
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-40"), pr(8, "HOME-41")]}, ci={"abc": "red"})
    gh.review_threads = lambda repo, n: (_ for _ in ()).throw(ValueError("boom")) if n == 7 else []
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.1, "x")])
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert [d["ref"] for d in report.declined] == ["HOME-40"]
    assert runner.briefs[0][0] == "bau-home-41"


def test_a_crashing_merged_pr_is_recorded_and_skipped(cfg, tmp_path):
    def merged_pr(n, ref):
        return PR(
            REPO,
            n,
            "n",
            f"fix ({ref})",
            "u",
            f"{ref.lower()}-x",
            "m",
            False,
            "t",
            "2026-10-14T12:00:00Z",
            "",
            "clean",
        )

    plane = FakePlane(
        {"HOME": [item(ref="HOME-40", state="In progress"), item(ref="HOME-41", state="In progress")]}
    )
    gh = FakeGitHub(open_prs={}, merged={REPO: [merged_pr(5, "HOME-40"), merged_pr(6, "HOME-41")]})
    repos = FakeRepos(tmp_path)
    original = repos.delete_checkout

    def flaky(branch):
        if branch == "home-40-x":
            raise OSError("disk")
        original(branch)

    repos.delete_checkout = flaky
    report = night(cfg, plane, gh, FakeRunner([]), repos).run()
    assert [d["ref"] for d in report.declined] == ["HOME-40"]
    assert ("set_state", ("HOME-41", "Ready for test")) in plane.calls


def test_leftover_checkout_from_a_failed_night_is_replaced(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]})
    repos = FakeRepos(tmp_path, existing=("home-42-rotate-logs",))
    runner = FakeRunner([SessionResult("failed", "no result line", good_rl(), 0.1, "")])
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, repos).run()
    assert report.sessions == 1 and report.declined == []
    names = [c[0] for c in repos.calls if c[0] in ("delete_checkout", "create_checkout")]
    assert names == ["delete_checkout", "create_checkout"]


def test_feedback_recreates_a_missing_checkout_from_the_remote_branch(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(
        open_prs={REPO: [pr(7, "HOME-40")]},
        threads={7: [Thread("T1", False, [ThreadComment("nakomis", "rename", "t")])]},
        ci={"abc": "green"},
    )
    repos = FakeRepos(tmp_path)
    night(cfg, plane, gh, FakeRunner([SessionResult("done", "", good_rl(), 0.1, "x")]), repos).run()
    assert ("checkout_branch", REPO, "home-40-rotate-logs") in repos.calls
    assert not any(c[0] == "rebase" for c in repos.calls)


def test_fix_session_rate_limited_ends_night_without_marker_or_wait(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(), item(ref="HOME-43", name="Other")]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "red"})
    sleeps = []
    runner = FakeRunner(
        [
            SessionResult("done", "", good_rl(), 0.5, "pr"),
            SessionResult("rate_limited", "limit", None, 0.1, ""),
        ],
        on_run=opens_pr(gh, pr()),
    )
    n = night(cfg, plane, gh, runner, FakeRepos(tmp_path))
    n.sleep = sleeps.append
    report = n.run()
    assert report.stop_reason == "rejected" and report.sessions == 2
    assert not any(c[0] == "comment" and "[bau:ci-fix]" in c[1][2] for c in gh.calls)
    assert len(sleeps) == 0
    assert not any(c[0] == "mark_ready" for c in gh.calls)


def test_failed_fix_session_still_counts_and_leaves_draft(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "red"})
    runner = FakeRunner(
        [
            SessionResult("done", "", good_rl(), 0.5, "pr"),
            SessionResult("failed", "exit 1", good_rl(), 0.1, ""),
        ],
        on_run=opens_pr(gh, pr()),
    )
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert ("comment", (REPO, 7, "[bau:ci-fix] 1/3")) in gh.calls
    assert not any(c[0] == "mark_ready" for c in gh.calls)


def test_too_big_fix_restores_the_true_previous_state(cfg, tmp_path):
    plane = FakePlane(
        {"HOME": [item(state="In progress")]},
        comments={"HOME-42": [Comment("c", "bau@nakomis.com", "", "[bau:picked-up from=Ready] x")]},
    )
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "red"})
    runner = FakeRunner(
        [
            SessionResult("done", "", good_rl(), 0.5, "pr"),
            SessionResult("too_big", "schema", good_rl(), 0.1, ""),
        ],
        on_run=opens_pr(gh, pr()),
    )
    repos = FakeRepos(tmp_path)
    night(cfg, plane, gh, runner, repos).run()
    states = [c[1][1] for c in plane.calls if c[0] == "set_state"]
    assert states == ["In progress", "Ready"]
    assert ("close_pr", (REPO, 7)) in gh.calls
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls


def test_draft_pr_with_green_ci_and_no_feedback_is_marked_ready_without_a_session(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-40", draft=True)]}, ci={"abc": "green"})
    runner = FakeRunner([])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert runner.briefs == []
    assert ("mark_ready", ("PR_7",)) in gh.calls
    assert ("comment", ("HOME-40", "[bau] PR #7 CI is now green; marked ready for review.")) in plane.calls


def test_non_draft_pr_with_green_ci_is_left_alone(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-40", draft=False)]}, ci={"abc": "green"})
    night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    assert not any(c[0] == "mark_ready" for c in gh.calls)


def test_feedback_session_then_green_ci_marks_ready(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(
        open_prs={REPO: [pr(7, "HOME-40", draft=True)]},
        threads={7: [Thread("T1", False, [ThreadComment("nakomis", "rename", "t")])]},
        ci={"abc": ["red", "pending", "green"]},
    )
    night(
        cfg, plane, gh, FakeRunner([SessionResult("done", "", good_rl(), 0.1, "x")]), FakeRepos(tmp_path)
    ).run()
    assert ("mark_ready", ("PR_7",)) in gh.calls


def test_repo_override_outside_projects_is_declined_before_any_github_call(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(desc="repo: nakomis/x\ndo it")]})
    gh = FakeGitHub(open_prs={REPO: []})
    report = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    assert report.declined[0]["reason"] == "repo override nakomis/x is not in projects.yaml"
    assert gh.calls == []
    assert any(
        c[0] == "comment" and "repo override nakomis/x is not in projects.yaml" in c[1][1]
        for c in plane.calls
    )


def test_merge_comment_carries_short_commit(cfg, tmp_path):
    merged = PR(
        REPO,
        5,
        "n",
        "fix (HOME-40)",
        "u",
        "home-40-x",
        "m",
        False,
        "t",
        "2026-10-14T12:00:00Z",
        "",
        "clean",
        merge_commit_sha="deadbeefcafe",
    )
    plane = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    night(
        cfg, plane, FakeGitHub(open_prs={}, merged={REPO: [merged]}), FakeRunner([]), FakeRepos(tmp_path)
    ).run()
    assert (
        "comment",
        ("HOME-40", "[bau] PR #5 merged at 2026-10-14T12:00:00Z as deadbee; moved to Ready for test."),
    ) in plane.calls
