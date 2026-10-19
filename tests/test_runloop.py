import json
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bau.config import Models, ProjectConfig
from bau.github import PR, Thread, ThreadComment, Token
from bau.markers import desc_hash, triage_comment
from bau.plane import Comment, Item
from bau.runloop import Night, branch_name, model_for, previous_state, repo_for
from bau.session import SessionResult
from bau.telemetry import RateLimit, Window
from tests.fakes.clients import FakeGitHub, FakePlane, FakeRepos, FakeRunner

LONDON = ZoneInfo("Europe/London")
REPO = "nakomis/home-infra"
PROJECTS = {"HOME": ProjectConfig("HOME", REPO, True, "Run the playbook")}


class TriagedPlane(FakePlane):
    """Every BAU item starts with a fresh ready verdict, so the night picks it up without triage."""

    def __init__(self, items, comments=None):
        comments = dict(comments or {})
        for it in (i for group in items.values() for i in group if "BAU" in i.labels):
            verdict = triage_comment("ready", 1, desc_hash(it.description), "ok")
            comments[it.ref] = [Comment("v", "bau@nakomis.com", "", verdict), *comments.get(it.ref, [])]
        super().__init__(items, comments)


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


class StubApp:
    def __init__(self):
        self.minted = 0

    def installation_token(self, repo):
        self.minted += 1
        return Token(f"ghs{self.minted}", "2026-10-15T03:00:00Z")


def night(
    cfg,
    plane,
    gh,
    runner,
    repos,
    hour=2,
    minute=0,
    dry_run=False,
    probe=None,
    app=None,
    paused=None,
    logs=None,
    projects=None,
    priority=None,
    atc=None,
):
    clock = lambda: datetime(2026, 10, 15, hour, minute, tzinfo=LONDON)  # noqa: E731
    n = Night(
        cfg,
        PROJECTS if projects is None else projects,
        plane,
        app or StubApp(),
        lambda repo: gh,
        repos,
        runner,
        clock,
        dry_run=dry_run,
        oauth_token="oat",
        log=(logs.append if logs is not None else lambda *_: None),
        sleep=lambda s: None,
        **({"paused": paused} if paused is not None else {}),
        **({"priority": priority} if priority is not None else {}),
        **({"atc": atc} if atc is not None else {}),
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
        previous_state(
            [Comment("c", "bau@nakomis.com", "", "[bau:picked-up from=Ready] branch x")], "bau@nakomis.com"
        )
        == "Ready"
    )


def test_ref_of_prefers_body_then_title():
    assert Night.ref_of(pr(ref="HOME-9")) == "HOME-9"
    bare = PR(REPO, 1, "n", "fix (BAU-3)", "u", "b", "s", True, "t", None, "no ref", "clean")
    assert Night.ref_of(bare) == "BAU-3"
    assert Night.ref_of(PR(REPO, 1, "n", "fix", "u", "b", "s", True, "t", None, "", "clean")) is None


def test_new_ticket_happy_path_opens_pr_and_marks_ready(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item()]})
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
    plane = TriagedPlane({"HOME": [item()]})
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
    plane = TriagedPlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "red"})
    runner = FakeRunner(
        [SessionResult("done", "", good_rl(), 0.5, "pr"), SessionResult("done", "", good_rl(), 0.2, "x")],
        on_run=opens_pr(gh, pr()),
    )
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert not any(c[0] == "mark_ready" for c in gh.calls)
    assert any(c[0] == "comment" and "draft" in c[1][1] and "red" in c[1][1] for c in plane.calls)


def test_previous_state_survives_a_repeat_pickup(cfg, tmp_path):
    plane = TriagedPlane(
        {"HOME": [item(state="In progress")]},
        comments={"HOME-42": [Comment("c", "bau@nakomis.com", "", "[bau:picked-up from=Ready] x")]},
    )
    runner = FakeRunner([SessionResult("too_big", "big", good_rl(), 0.1, "BAU_TOO_BIG: big")])
    night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, FakeRepos(tmp_path)).run()
    assert ("set_state", ("HOME-42", "Ready")) in plane.calls
    assert any(c[0] == "comment" and "[bau:picked-up from=Ready]" in c[1][1] for c in plane.calls)


def test_diff_over_threshold_is_closed_as_too_big_even_if_agent_opened_a_pr(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "green"})
    runner = FakeRunner(
        [SessionResult("done", "", good_rl(), 0.5, "Opened https://x/pull/7")],
        on_run=opens_pr(gh, replace(pr(), changed_files=20, additions=40, deletions=10)),
    )
    repos = FakeRepos(tmp_path)
    report = night(cfg, plane, gh, runner, repos).run()
    assert ("close_pr", (REPO, 7)) in gh.calls
    assert not any(c[0] == "mark_ready" for c in gh.calls)
    assert ("set_state", ("HOME-42", "New")) in plane.calls
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls
    assert report.prs_opened == 0


def test_feedback_first_runs_before_new_tickets_and_acks(cfg, tmp_path):
    existing = pr(number=7, ref="HOME-40")
    plane = TriagedPlane(
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
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
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


def test_declines_unprotected_and_empty_and_skips_unmapped_silently(cfg, tmp_path):
    plane = TriagedPlane(
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
    assert {d["ref"] for d in report.declined} == {"HOME-42", "HOME-43"}
    reasons = {c[1][0]: c[1][1] for c in plane.calls if c[0] == "comment"}
    assert set(reasons) == {"HOME-42", "HOME-43"}
    assert all(v.startswith("[bau] Not picked up:") for v in reasons.values())
    assert all(c[0] != "set_state" for c in plane.calls)


def test_decline_comment_is_not_repeated_on_later_nights(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(desc="")]})
    night(cfg, plane, FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path)).run()
    text = next(c[1][1] for c in plane.calls if c[0] == "comment")
    plane.comments_by_ref["HOME-42"] = [Comment("c", "bau@nakomis.com", "", text)]
    plane.calls.clear()
    night(cfg, plane, FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path)).run()
    assert plane.calls == []


def test_pace_fails_at_preflight(cfg, tmp_path):
    now = datetime(2026, 10, 15, 2, tzinfo=LONDON).timestamp()
    bad = RateLimit("allowed", Window(0.1, int(now + 3600)), Window(0.9, int(now + 300000)))
    plane = TriagedPlane({"HOME": [item()]})
    report = night(cfg, plane, FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path), probe=bad).run()
    assert report.stop_reason == "pace" and report.sessions == 0


def test_rejected_ends_the_night_at_once(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(), item(ref="HOME-43", name="Other")]})
    gh = FakeGitHub(open_prs={REPO: []})
    runner = FakeRunner([SessionResult("rate_limited", "hit your limit", None, 0.1, "hit your limit")])
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert report.sessions == 1 and report.stop_reason == "rejected"


def test_dry_run_writes_nothing(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item()]})
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
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
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
    plane = TriagedPlane({"BAU": [item(ref="BAU-4", state="In progress")]})
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
    plane = TriagedPlane({"HOME": [item(state="Done")]})
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), FakeRunner([]), FakeRepos(tmp_path)).run()
    assert report.sessions == 0 and plane.calls == []


def test_wait_for_ci_polls_until_settled(cfg, tmp_path):
    sleeps = []
    gh = FakeGitHub(ci={"abc": ["pending", "none", "green"]})
    n = night(cfg, TriagedPlane({}), gh, FakeRunner([]), FakeRepos(tmp_path))
    n.sleep = sleeps.append
    assert n.wait_for_ci(gh, REPO, "abc", poll_s=30) == "green"
    assert sleeps == [30, 30]


def test_wait_for_ci_times_out_with_last_status(cfg, tmp_path):
    sleeps = []
    gh = FakeGitHub(ci={"abc": "pending"})
    n = night(cfg, TriagedPlane({}), gh, FakeRunner([]), FakeRepos(tmp_path))
    n.sleep = sleeps.append
    assert n.wait_for_ci(gh, REPO, "abc", timeout_s=90, poll_s=30) == "pending"
    assert sleeps == [30, 30, 30]


def test_tokens_are_reminted_when_close_to_expiry(cfg, tmp_path):
    app = StubApp()
    n = night(cfg, TriagedPlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path), app=app)
    assert n.token(REPO) == "ghs1" and n.token(REPO) == "ghs1"
    n.clock = lambda: datetime(2026, 10, 15, 3, 55, tzinfo=LONDON)  # 02:55 UTC, token expires 03:00
    assert n.token(REPO) == "ghs2"


def test_probe_uses_subscription_login_and_never_bare(cfg, tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("bau.probe.subprocess.run", fake_run)
    n = night(cfg, TriagedPlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path))
    assert Night.probe(n) is None
    assert "--disable-slash-commands" in seen["cmd"] and "--bare" not in seen["cmd"]
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oat"


def test_a_crashing_ticket_does_not_abort_the_night(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(), item(ref="HOME-43", name="Other")]})
    gh = FakeGitHub(open_prs={REPO: []})
    runner = FakeRunner([RuntimeError("docker exploded"), SessionResult("done", "", good_rl(), 0.1, "x")])
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert [b[0] for b in runner.briefs] == ["bau-home-42", "bau-home-43"]
    assert report.sessions == 2
    failed = [d for d in report.declined if d["ref"] == "HOME-42"]
    assert len(failed) == 1 and failed[0]["reason"] == "error: RuntimeError: docker exploded"
    assert any(c[0] == "comment" and "Error while working this ticket" in c[1][1] for c in plane.calls)


def test_a_crashing_pr_in_the_feedback_pass_is_recorded_and_skipped(cfg, tmp_path):
    plane = TriagedPlane(
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

    plane = TriagedPlane(
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
    plane = TriagedPlane({"HOME": [item()]})
    repos = FakeRepos(tmp_path, existing=("home-42-rotate-logs",))
    runner = FakeRunner([SessionResult("failed", "no result line", good_rl(), 0.1, "")])
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, repos).run()
    assert report.sessions == 1 and report.declined == []
    names = [c[0] for c in repos.calls if c[0] in ("delete_checkout", "create_checkout")]
    assert names == ["delete_checkout", "create_checkout"]


def test_feedback_recreates_a_missing_checkout_from_the_remote_branch(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(
        open_prs={REPO: [pr(7, "HOME-40")]},
        threads={7: [Thread("T1", False, [ThreadComment("nakomis", "rename", "t")])]},
        ci={"abc": "green"},
    )
    repos = FakeRepos(tmp_path)
    night(cfg, plane, gh, FakeRunner([SessionResult("done", "", good_rl(), 0.1, "x")]), repos).run()
    assert ("checkout_branch", REPO, "home-40-rotate-logs") in repos.calls


def test_fix_session_rate_limited_ends_night_without_marker_or_wait(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(), item(ref="HOME-43", name="Other")]})
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
    plane = TriagedPlane({"HOME": [item()]})
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
    plane = TriagedPlane(
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
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-40", draft=True)]}, ci={"abc": "green"})
    runner = FakeRunner([])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert runner.briefs == []
    assert ("mark_ready", ("PR_7",)) in gh.calls
    assert ("comment", ("HOME-40", "[bau] PR #7 CI is now green; marked ready for review.")) in plane.calls


def test_non_draft_pr_with_green_ci_is_left_alone(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-40", draft=False)]}, ci={"abc": "green"})
    night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    assert not any(c[0] == "mark_ready" for c in gh.calls)


def test_feedback_session_then_green_ci_marks_ready(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
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
    plane = TriagedPlane({"HOME": [item(desc="repo: nakomis/x\ndo it")]})
    gh = FakeGitHub(open_prs={REPO: []})
    report = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    assert report.declined[0]["reason"] == "repo override nakomis/x is not a BAU project in ATC"
    assert gh.calls == []
    assert any(
        c[0] == "comment" and "repo override nakomis/x is not a BAU project in ATC" in c[1][1]
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
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    night(
        cfg, plane, FakeGitHub(open_prs={}, merged={REPO: [merged]}), FakeRunner([]), FakeRepos(tmp_path)
    ).run()
    assert (
        "comment",
        ("HOME-40", "[bau] PR #5 merged at 2026-10-14T12:00:00Z as deadbee; moved to Ready for test."),
    ) in plane.calls


def test_green_draft_over_the_size_limit_is_closed_not_marked_ready(cfg, tmp_path):
    big = replace(pr(7, "HOME-40", draft=True), changed_files=20)
    plane = TriagedPlane(
        {"HOME": [item(ref="HOME-40", state="In progress")]},
        comments={"HOME-40": [Comment("c", "bau@nakomis.com", "", "[bau:picked-up from=Ready] x")]},
    )
    gh = FakeGitHub(open_prs={REPO: [big]}, ci={"abc": "green"})
    repos = FakeRepos(tmp_path)
    runner = FakeRunner([])
    night(cfg, plane, gh, runner, repos).run()
    assert runner.briefs == []
    assert not any(c[0] == "mark_ready" for c in gh.calls)
    assert ("close_pr", (REPO, 7)) in gh.calls
    assert ("set_state", ("HOME-40", "Ready")) in plane.calls
    assert ("delete_checkout", "home-40-rotate-logs") in repos.calls


def test_error_comment_is_not_repeated_and_failed_ticket_returns_to_its_state(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(state="New")]})
    repos = FakeRepos(tmp_path)
    for _ in range(2):
        gh = FakeGitHub(open_prs={REPO: []})
        report = night(cfg, plane, gh, FakeRunner([RuntimeError("docker exploded")]), repos).run()
        assert report.declined[0]["reason"] == "error: RuntimeError: docker exploded"
    texts = [c[1][1] for c in plane.calls if c[0] == "comment"]
    assert sum("Error while working this ticket" in t for t in texts) == 1
    assert sum("[bau:picked-up" in t for t in texts) == 1
    assert plane.get_item("HOME-42").state == "New"


def test_feedback_pass_quietly_skips_a_pr_whose_ref_does_not_resolve(cfg, tmp_path):
    plane = TriagedPlane({"HOME": []})
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-99")]}, ci={"abc": "red"})
    report = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    assert plane.calls == [] and report.declined == []


# -- final fix wave ---------------------------------------------------------------------------------

FEEDBACK_THREAD = {7: [Thread("T1", False, [ThreadComment("nakomis", "rename", "t")])]}


def test_feedback_session_always_gets_a_fresh_clone_and_rebases_inside_the_container(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-40")]}, threads=FEEDBACK_THREAD, ci={"abc": "green"})
    repos = FakeRepos(tmp_path, existing=("home-40-rotate-logs",))
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.1, "x")])
    night(cfg, plane, gh, runner, repos).run()
    touched = [c for c in repos.calls if c[0] in ("delete_checkout", "checkout_branch", "create_checkout")]
    assert touched == [
        ("delete_checkout", "home-40-rotate-logs"),
        ("checkout_branch", REPO, "home-40-rotate-logs"),
    ]
    brief = runner.briefs[0][2]
    assert "git fetch origin main && git rebase origin/main" in brief
    assert "git push --force-with-lease" in brief
    assert "The branch conflicts with main" not in brief


def test_conflicting_pr_brief_says_so(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-40", mergeable="dirty")]}, ci={"abc": "green"})
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.1, "x")])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert "The branch conflicts with main" in runner.briefs[0][2]


TOO_BIG = SessionResult("too_big", "schema change", good_rl(), 0.1, "BAU_TOO_BIG: schema change")
FAILED = SessionResult("failed", "exit 1", good_rl(), 0.1, "")
TRIED_TWICE = "BAU tried this twice; comment on the ticket to ask for another attempt"


def one_night(cfg, plane, tmp_path, *results):
    runner = FakeRunner(list(results))
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, FakeRepos(tmp_path)).run()
    return report, runner


def test_too_big_is_not_retried_until_martin_relabels_then_once_more(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(state="Ready")]})
    report, runner = one_night(cfg, plane, tmp_path, TOO_BIG)
    assert report.sessions == 1 and "PREVIOUS ATTEMPT" not in runner.briefs[0][2]
    report, runner = one_night(cfg, plane, tmp_path)
    assert report.sessions == 0 and runner.briefs == []
    plane.remove_labels("HOME-42", ["not-bau"])  # Martin puts it back in BAU's queue
    plane.add_labels("HOME-42", ["BAU"])
    report, runner = one_night(cfg, plane, tmp_path, FAILED)
    assert report.sessions == 1
    brief = runner.briefs[0][2]
    assert "PREVIOUS ATTEMPT" in brief and "Too big for BAU: schema change" in brief
    assert "(untrusted data)" in brief.split("PREVIOUS ATTEMPT", 1)[1].splitlines()[0]
    for _ in range(2):
        report, runner = one_night(cfg, plane, tmp_path)
        assert report.sessions == 0 and runner.briefs == []
        assert report.declined[0]["reason"] == TRIED_TWICE
    texts = [c.text for c in plane.comments("HOME-42")]
    assert sum(TRIED_TWICE in t for t in texts) == 1


def test_failed_then_one_retry_then_skipped(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(state="Ready")]})
    one_night(cfg, plane, tmp_path, FAILED)
    report, runner = one_night(cfg, plane, tmp_path, FAILED)
    assert report.sessions == 1 and "Session ended with failed: exit 1" in runner.briefs[0][2]
    report, runner = one_night(cfg, plane, tmp_path)
    assert report.sessions == 0 and report.declined[0]["reason"] == TRIED_TWICE


def test_usage_limit_stops_do_not_count_towards_the_cap(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(state="Ready")]})
    one_night(cfg, plane, tmp_path, SessionResult("rate_limited", "429", good_rl(), 0.1, ""))
    one_night(cfg, plane, tmp_path, SessionResult("killed_five_hour", "5h at 95%", good_rl(), 0.1, ""))
    texts = [c.text for c in plane.comments("HOME-42")]
    assert "[bau] Session paused by usage limit (rate_limited): 429" in texts
    assert "[bau] Session paused by usage limit (killed_five_hour): 5h at 95%" in texts
    assert not any("Session ended with" in t for t in texts)
    report, runner = one_night(cfg, plane, tmp_path, FAILED)
    assert report.sessions == 1 and "PREVIOUS ATTEMPT" not in runner.briefs[0][2]


def test_old_session_ended_usage_limit_comments_do_not_count(cfg, tmp_path):
    old = [
        Comment(f"o{n}", "bau@nakomis.com", "", f"[bau] Session ended with {outcome}: x")
        for n, outcome in enumerate(("rate_limited", "killed_five_hour"))
    ]
    plane = TriagedPlane({"HOME": [item(state="Ready")]}, {"HOME-42": old})
    report, _ = one_night(cfg, plane, tmp_path, FAILED)
    assert report.sessions == 1


def test_wall_clock_kills_still_count_towards_the_cap(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(state="Ready")]})
    for _ in range(2):
        one_night(cfg, plane, tmp_path, SessionResult("killed_wall_clock", "1h", good_rl(), 0.1, ""))
    report, runner = one_night(cfg, plane, tmp_path)
    assert report.sessions == 0 and report.declined[0]["reason"] == TRIED_TWICE


def test_tried_twice_hands_the_ticket_back(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(state="Ready")]})
    one_night(cfg, plane, tmp_path, FAILED)
    one_night(cfg, plane, tmp_path, FAILED)
    held = plane.get_item("HOME-42")
    assert held.state == "In progress" and held.assignees == ["bau@nakomis.com"]
    one_night(cfg, plane, tmp_path)
    back = plane.get_item("HOME-42")
    assert back.state == "Ready" and back.assignees == []
    plane.calls.clear()
    report, _ = one_night(cfg, plane, tmp_path)  # already handed back: no further writes
    assert report.declined[0]["reason"] == TRIED_TWICE and plane.calls == []


def test_owner_comment_after_two_failures_allows_another_attempt(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(state="Ready")]})
    one_night(cfg, plane, tmp_path, FAILED)
    one_night(cfg, plane, tmp_path, FAILED)
    plane.comments_by_ref["HOME-42"].append(Comment("o1", "martin@nakomis.com", "", "please try again"))
    report, runner = one_night(cfg, plane, tmp_path, FAILED)
    assert report.sessions == 1 and "PREVIOUS ATTEMPT" not in runner.briefs[0][2]


def merged_pr(number=5, ref="HOME-40", head=None):
    return PR(
        REPO,
        number,
        f"PR_{number}",
        f"fix ({ref})",
        f"https://x/pull/{number}",
        head or f"{ref.lower()}-x",
        "m",
        False,
        "2026-10-01T01:00:00Z",
        "2026-10-02T12:00:00Z",
        f"Plane: {ref}",
        "clean",
    )


def test_a_night_stopped_by_pace_still_closes_merges_and_marks_green_drafts_ready(cfg, tmp_path):
    now = datetime(2026, 10, 15, 2, tzinfo=LONDON).timestamp()
    bad = RateLimit("allowed", Window(0.1, int(now + 3600)), Window(0.9, int(now + 300000)))
    plane = TriagedPlane(
        {"HOME": [item(ref="HOME-40", state="In progress"), item(ref="HOME-41", state="In progress")]}
    )
    gh = FakeGitHub(
        open_prs={REPO: [pr(8, "HOME-41", draft=True)]}, ci={"abc": "green"}, merged={REPO: [merged_pr()]}
    )
    report = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path), probe=bad).run()
    assert report.stop_reason == "pace" and report.sessions == 0
    assert ("set_state", ("HOME-40", "Ready for test")) in plane.calls
    assert ("mark_ready", ("PR_8",)) in gh.calls


def test_merged_prs_are_looked_for_over_thirty_days(cfg, tmp_path):
    gh = FakeGitHub(open_prs={REPO: []})
    night(cfg, TriagedPlane({"HOME": []}), gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    assert gh.merged_since == ["2026-09-15T00:00:00Z"]


def test_an_item_with_a_merged_pr_is_never_picked_up_again(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: []}, merged={REPO: [merged_pr()]})
    runner = FakeRunner([])
    n = night(cfg, plane, gh, runner, FakeRepos(tmp_path))
    n._open = {REPO: []}
    n.merge_pass()
    plane.items["HOME"] = [item(ref="HOME-40", state="Ready")]  # moved back by hand after the merge
    plane.calls.clear()
    n.new_tickets_pass()
    assert runner.briefs == []
    assert ("set_state", ("HOME-40", "In progress")) not in plane.calls
    assert ("set_state", ("HOME-40", "Ready for test")) in plane.calls


def test_pr_whose_branch_does_not_match_its_ref_is_never_acted_on(cfg, tmp_path):
    forged = replace(pr(7, "HOME-40", draft=True), head_ref="evil-branch")
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(
        open_prs={REPO: [forged]},
        threads=FEEDBACK_THREAD,
        ci={"abc": "green"},
        merged={REPO: [merged_pr(head="other-x")]},
    )
    logs: list = []
    runner = FakeRunner([])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path), logs=logs).run()
    assert runner.briefs == [] and gh.calls == []
    assert not any(c[0] == "set_state" for c in plane.calls)
    assert any("does not match" in str(m) for m in logs)


def test_a_session_without_telemetry_drops_the_stale_reading(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item()]})
    runner = FakeRunner([SessionResult("failed", "exit 1", None, 0.1, "")])
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, FakeRepos(tmp_path)).run()
    assert report.latest is None and report.telemetry == "missing"


def test_preflight_failure_on_plane_stops_the_night(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item()]})
    plane.unreachable = RuntimeError("Plane GET /projects/ -> 502")
    runner = FakeRunner([])
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, FakeRepos(tmp_path)).run()
    assert report.stop_reason == "preflight: RuntimeError" and runner.briefs == []


def test_preflight_failure_on_github_stops_the_night(cfg, tmp_path):
    class DownApp:
        def installation_token(self, repo):
            raise ConnectionError("no route")

    report = night(
        cfg,
        TriagedPlane({"HOME": [item()]}),
        FakeGitHub(),
        FakeRunner([]),
        FakeRepos(tmp_path),
        app=DownApp(),
    ).run()
    assert report.stop_reason == "preflight: ConnectionError" and report.sessions == 0


def test_pause_is_rechecked_before_every_session(cfg, tmp_path):
    flag = [False]
    plane = TriagedPlane({"HOME": [item(), item(ref="HOME-43", name="Other")]})
    runner = FakeRunner([FAILED, FAILED], on_run=lambda: flag.__setitem__(0, True))
    report = night(
        cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, FakeRepos(tmp_path), paused=lambda: flag[0]
    ).run()
    assert report.sessions == 1 and report.stop_reason == "paused"


def test_wait_for_ci_gives_no_checks_a_grace_then_returns_none(cfg, tmp_path):
    sleeps = []
    gh = FakeGitHub(ci={"abc": "none"})
    n = night(cfg, TriagedPlane({}), gh, FakeRunner([]), FakeRepos(tmp_path))
    n.sleep = sleeps.append
    assert n.wait_for_ci(gh, REPO, "abc", poll_s=30) == "none"
    assert sum(sleeps) == 180


def test_pr_with_no_ci_is_marked_ready_with_a_comment(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []})
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.5, "pr")], on_run=opens_pr(gh, pr()))
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert ("mark_ready", ("PR_7",)) in gh.calls
    assert (
        "comment",
        (REPO, 7, "[bau] No CI checks ran on this repository; marked ready without them."),
    ) in gh.calls


def test_draft_with_no_ci_from_an_earlier_night_is_marked_ready(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(open_prs={REPO: [pr(7, "HOME-40", draft=True)]})
    night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    assert ("mark_ready", ("PR_7",)) in gh.calls
    assert any(c[0] == "comment" and "No CI checks ran" in c[1][2] for c in gh.calls)


def write_fake_claude(bindir: Path) -> None:
    bindir.mkdir(parents=True)
    event = (
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":'
        '{"five_hour":{"utilization":0.2,"resetsAt":1},"seven_day":{"utilization":0.3,"resetsAt":2}}}}'
    )
    script = bindir / "claude"
    script.write_text(f"#!/bin/sh\necho '{event}'\n")
    script.chmod(0o755)


def test_probe_finds_claude_in_local_bin_and_keeps_the_environment(cfg, tmp_path, monkeypatch):
    home = tmp_path / "home"
    write_fake_claude(home / ".local" / "bin")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    n = night(cfg, TriagedPlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path))
    rl = Night.probe(n)
    assert rl is not None and rl.seven_day == Window(0.3, 2)


def test_probe_reports_a_missing_claude_loudly(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    logs: list = []
    n = night(cfg, TriagedPlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path), logs=logs)
    assert Night.probe(n) is None
    assert any("claude" in str(m) and "not found" in str(m) for m in logs)
    n.probe = lambda: Night.probe(n)
    assert n.run().telemetry == "missing"


def test_previous_state_ignores_non_worker_comments():
    comments = [
        Comment("c1", "bau@nakomis.com", "", "[bau:picked-up from=Ready] x"),
        Comment("c2", "martin@nakomis.com", "", "[bau:picked-up from=Done] x"),
    ]
    assert previous_state(comments, "bau@nakomis.com") == "Ready"


def test_already_picked_up_recognises_a_round_tripped_comment(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item()]})
    plane.comment("HOME-42", "[bau:picked-up from=Ready] Working on branch `home-42-x` in x.")
    n = night(cfg, plane, FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path))
    assert n.already_picked_up("HOME-42", "home-42-x", "Ready")
    assert not n.already_picked_up("HOME-42", "home-42-y", "Ready")


# -- night integration: sweep, triage, installation, not-bau --------------------
def ready_verdict(it: Item) -> Comment:
    return Comment("v", "bau@nakomis.com", "", triage_comment("ready", 1, desc_hash(it.description), "ok"))


def triage_session(payload: dict) -> SessionResult:
    return SessionResult("done", "", good_rl(), 0.2, f"```json\n{json.dumps(payload)}\n```")


def test_run_order_sweep_then_triage_then_pickup(cfg, tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr("bau.sweep.sweep_pass", lambda n: order.append("sweep"))
    monkeypatch.setattr("bau.triage.triage_pass", lambda n: order.append("triage"))
    n = night(cfg, FakePlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path))
    for name in ("merge_pass", "ready_pass", "feedback_pass", "new_tickets_pass"):
        monkeypatch.setattr(n, name, lambda name=name: order.append(name))
    n.run()
    assert order == ["merge_pass", "ready_pass", "feedback_pass", "sweep", "triage", "new_tickets_pass"]


def test_a_pass_that_ends_the_night_stops_the_later_ones(cfg, tmp_path, monkeypatch):
    order = []

    def rejected(n):
        order.append("sweep")
        n.report.stop_reason = "rejected"

    monkeypatch.setattr("bau.sweep.sweep_pass", rejected)
    monkeypatch.setattr("bau.triage.triage_pass", lambda n: order.append("triage"))
    n = night(cfg, FakePlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path))
    monkeypatch.setattr(n, "new_tickets_pass", lambda: order.append("new_tickets_pass"))
    assert n.run().stop_reason == "rejected" and order == ["sweep"]


def test_untriaged_bau_item_is_not_picked_up(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("bau.triage.triage_pass", lambda n: None)
    plane = FakePlane({"HOME": [item()]})
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), FakeRunner([]), FakeRepos(tmp_path)).run()
    assert report.sessions == 0 and plane.calls == [] and report.declined == []


def test_items_waiting_or_not_bau_are_not_picked_up_even_with_a_ready_verdict(cfg, tmp_path):
    items = [item(labels=("BAU", "bau-questions")), item(ref="HOME-43", labels=("BAU", "not-bau"))]
    plane = FakePlane({"HOME": items}, comments={i.ref: [ready_verdict(i)] for i in items})
    report = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), FakeRunner([]), FakeRepos(tmp_path)).run()
    assert report.sessions == 0 and plane.calls == []


def test_triaged_ready_item_is_picked_up_same_night(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "green"})
    runs = []

    def on_run():
        runs.append(1)
        if len(runs) == 2:
            gh.open[REPO] = [pr()]

    runner = FakeRunner(
        [
            triage_session({"verdict": "ready", "reason": "clear", "summary": "s"}),
            SessionResult("done", "", good_rl(), 0.5, "Opened"),
        ],
        on_run=on_run,
    )
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert [b[0] for b in runner.briefs] == ["bau-triage-home-42", "bau-home-42"]
    assert report.triaged == 1 and report.prs_opened == 1
    texts = [c.text for c in plane.comments("HOME-42")]
    assert texts[0].startswith("[bau:triage verdict=ready round=1")
    assert texts[1].startswith("[bau:picked-up from=New]")


def test_uninstalled_repo_does_not_end_the_night(cfg, tmp_path):
    other = "nakomis/other"
    projects = PROJECTS | {
        "OTHER": ProjectConfig("OTHER", other, False),
        "NONE": ProjectConfig("NONE", None, False),
    }

    class PartlyInstalled(StubApp):
        def installation_token(self, repo):
            if repo == other:
                raise RuntimeError("no installation for nakomis/other")
            return super().installation_token(repo)

    items = [item(), item(ref="OTHER-1"), item(ref="NONE-1")]
    plane = FakePlane(
        {"HOME": items[:1], "OTHER": items[1:2], "NONE": items[2:]},
        comments={i.ref: [ready_verdict(i)] for i in items},
    )
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "green"})
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.5, "Opened")], on_run=opens_pr(gh, pr()))
    repos = FakeRepos(tmp_path)
    logs = []
    n = night(cfg, plane, gh, runner, repos, app=PartlyInstalled(), logs=logs, projects=projects)
    report = n.run()
    assert report.stop_reason == "ok" and report.uninstalled == [other]
    assert n.installed == {REPO}
    assert [b[0] for b in runner.briefs] == ["bau-home-42"] and report.prs_opened == 1
    assert ("ensure_bare", other) not in repos.calls
    assert not any(c[1][0] in ("OTHER-1", "NONE-1") for c in plane.calls)
    assert report.declined == []
    assert any(other in line for line in logs)


def test_too_big_marks_not_bau(cfg, tmp_path):
    plane = FakePlane({"HOME": [item(state="Ready")]}, comments={"HOME-42": [ready_verdict(item())]})
    runner = FakeRunner([SessionResult("too_big", "schema change", good_rl(), 0.1, "BAU_TOO_BIG: schema")])
    repos = FakeRepos(tmp_path)
    night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, repos).run()
    writes = [c for c in plane.calls if c[0] in ("add_labels", "remove_labels", "comment")][-3:]
    assert writes == [
        ("add_labels", ("HOME-42", ["not-bau"])),
        ("comment", ("HOME-42", "[bau] Too big for BAU: schema change. Returned to Ready. Marked not-bau.")),
        ("remove_labels", ("HOME-42", ["BAU"])),
    ]
    assert ("set_state", ("HOME-42", "Ready")) in plane.calls
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls
    assert plane.get_item("HOME-42").labels == ["not-bau"]


def test_closing_a_too_big_pr_marks_not_bau(cfg, tmp_path):
    big = replace(pr(7, "HOME-40", draft=True), changed_files=20)
    plane = FakePlane(
        {"HOME": [item(ref="HOME-40", state="In progress")]},
        comments={"HOME-40": [Comment("c", "bau@nakomis.com", "", "[bau:picked-up from=Ready] x")]},
    )
    gh = FakeGitHub(open_prs={REPO: [big]}, ci={"abc": "green"})
    night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    writes = [c for c in plane.calls if c[0] in ("add_labels", "remove_labels", "comment")]
    assert writes == [
        ("add_labels", ("HOME-40", ["not-bau"])),
        (
            "comment",
            ("HOME-40", "[bau] Too big for BAU (20 files / 12 lines). Returned to Ready. Marked not-bau."),
        ),
        ("remove_labels", ("HOME-40", ["BAU"])),
    ]


class InstalledOn(StubApp):
    def __init__(self, *repos):
        super().__init__()
        self.repos = repos

    def installation_token(self, repo):
        if repo not in self.repos:
            raise RuntimeError(f"no installation for {repo}")
        return super().installation_token(repo)


def test_first_projects_repo_uninstalled_still_runs_the_installed_repos_work(cfg, tmp_path):
    first = "nakomis/aardvark"  # first in projects.yaml order and in sorted order
    projects = {"AARD": ProjectConfig("AARD", first, False)} | PROJECTS
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [ready_verdict(item())]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "green"})
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.5, "Opened")], on_run=opens_pr(gh, pr()))
    n = night(cfg, plane, gh, runner, FakeRepos(tmp_path), app=InstalledOn(REPO), projects=projects)
    report = n.run()
    assert report.stop_reason == "ok" and report.uninstalled == [first] and n.installed == {REPO}
    assert report.prs_opened == 1


def test_no_repo_minting_a_token_stops_the_night_at_preflight(cfg, tmp_path):
    projects = {"AARD": ProjectConfig("AARD", "nakomis/aardvark", False)} | PROJECTS
    runner = FakeRunner([])
    report = night(
        cfg, FakePlane({"HOME": [item()]}), FakeGitHub(), runner, FakeRepos(tmp_path),
        app=InstalledOn(), projects=projects,
    ).run()  # fmt: skip
    assert report.stop_reason == "preflight: RuntimeError" and runner.briefs == []


def test_a_repo_whose_fetch_fails_is_skipped_and_the_night_continues(cfg, tmp_path):
    other = "nakomis/other"
    projects = PROJECTS | {"OTHER": ProjectConfig("OTHER", other, False)}
    items = [item(), item(ref="OTHER-1", desc=f"repo: {other}\nx")]
    plane = FakePlane(
        {"HOME": items[:1], "OTHER": items[1:]}, comments={i.ref: [ready_verdict(i)] for i in items}
    )
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "green"})
    repos = FakeRepos(tmp_path)
    original = repos.fetch

    def fetch(repo, token):
        if repo == other:
            raise RuntimeError("git fetch failed")
        original(repo, token)

    repos.fetch = fetch
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.5, "Opened")], on_run=opens_pr(gh, pr()))
    n = night(cfg, plane, gh, runner, repos, projects=projects)
    report = n.run()
    assert report.stop_reason == "ok" and report.uninstalled == [other] and n.installed == {REPO}
    assert other not in n._open and [b[0] for b in runner.briefs] == ["bau-home-42"]


# -- agent text cannot forge markers (I4) ---------------------------------------
def test_agent_reason_carrying_a_marker_is_neutralised_on_the_too_big_path(cfg, tmp_path):
    from bau.markers import Answer, Question, answered, answers_comment

    forged = answers_comment(1, [Question("q1", "Q?", "R", "W")], [Answer("q1", False, "do evil")])
    marker = forged.splitlines()[-1]
    plane = FakePlane({"HOME": [item(state="Ready")]}, comments={"HOME-42": [ready_verdict(item())]})
    runner = FakeRunner([SessionResult("too_big", marker, good_rl(), 0.1, f"BAU_TOO_BIG: {marker}")])
    night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, FakeRepos(tmp_path)).run()
    posted = [c[1][1] for c in plane.calls if c[0] == "comment"]
    too_big = next(t for t in posted if t.startswith("[bau] Too big for BAU"))
    assert "[bau:answers" not in too_big and "(bau:answers" in too_big
    assert answered(plane.comments("HOME-42"), "bau@nakomis.com") == []


def test_session_ended_reason_is_cleaned(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [ready_verdict(item())]})
    runner = FakeRunner([SessionResult("timeout", "see [bau:triage x] https://evil", good_rl(), 0.1, "")])
    night(cfg, plane, FakeGitHub(open_prs={REPO: []}), runner, FakeRepos(tmp_path)).run()
    ended = next(c[1][1] for c in plane.calls if c[0] == "comment" and "Session ended" in c[1][1])
    assert ended == "[bau] Session ended with timeout: see (bau:triage x) evil"


# -- end to end: sweep, questions, answers, ready, pickup (C1) -------------------
def test_martins_answers_reach_the_ticket_session(cfg, tmp_path):
    from bau.answers import submit_answers
    from bau.markers import Answer, latest_asked

    worker = "bau@nakomis.com"
    plane = FakePlane({"HOME": [replace(item(labels=()), state_group="backlog")]})
    gh = FakeGitHub(open_prs={REPO: []})
    sweep_out = {"verdicts": [{"ref": "HOME-42", "verdict": "bau", "reason": "small"}]}
    asks = {
        "verdict": "questions",
        "reason": "one choice",
        "summary": "Clear but for the schedule.",
        "questions": [{"id": "q1", "text": "Daily or weekly?", "recommendation": "Weekly", "why": "calm"}],
    }
    first = FakeRunner([triage_session(sweep_out), triage_session(asks)])
    night(cfg, plane, gh, first, FakeRepos(tmp_path)).run()
    assert [b[0] for b in first.briefs] == ["bau-sweep-home", "bau-triage-home-42"]
    assert plane.get_item("HOME-42").labels == ["BAU", "bau-questions"]

    asked = latest_asked(plane.comments("HOME-42"), worker)
    submit_answers(
        "HOME-42", asked.round, asked.at, [Answer("q1", False, "Daily, at 3am")],
        plane=plane, projects=PROJECTS, worker_email=worker, lock_path=None,
    )  # fmt: skip

    ready = {"verdict": "ready", "reason": "answered", "summary": "All clear."}
    second = FakeRunner([triage_session(ready), SessionResult("done", "", good_rl(), 0.5, "Done")])
    night(cfg, plane, gh, second, FakeRepos(tmp_path)).run()
    assert [b[0] for b in second.briefs] == ["bau-triage-home-42", "bau-home-42"]
    ticket_brief = second.briefs[1][2]
    assert "[owner] Daily, at 3am (Martin's own words)" in ticket_brief
    assert "[other] Question q1: Daily or weekly?" in ticket_brief


def test_feedback_session_brief_carries_the_questions_history(cfg, tmp_path):
    from bau.markers import Answer, Question, answers_comment, questions_comment

    qs = [Question("q1", "Daily or weekly?", "Weekly", "calm")]
    history = [
        Comment("q", "bau@nakomis.com", "", questions_comment(1, "Gap.", qs)),
        Comment("a", "bau@nakomis.com", "", answers_comment(1, qs, [Answer("q1", True, "Weekly")])),
    ]
    plane, gh = red_pr_world([])
    plane.comments_by_ref["HOME-40"] += history
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.3, "fixed")])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert "[owner] Weekly (recommendation accepted)" in runner.briefs[0][2]


def gh_feedback_world(rows=(), reviews=()):
    existing = pr(number=7, ref="HOME-40")
    plane = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    gh = FakeGitHub(
        open_prs={REPO: [existing]},
        ci={"abc": "green"},
        comment_rows={7: list(rows)},
        reviews={7: list(reviews)},
    )
    return plane, gh


def test_owner_pr_conversation_comment_triggers_feedback_and_is_acked_once(cfg, tmp_path):
    from bau.github import IssueComment

    plane, gh = gh_feedback_world(rows=[IssueComment(21, "nakomis", "please tidy the docs")])
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.3, "ok")])
    report = night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert "PR COMMENT 21" in runner.briefs[0][2] and "[owner] please tidy the docs" in runner.briefs[0][2]
    assert any(c[0] == "comment" and "[bau:ack-gh issue:21]" in c[1][2] for c in gh.calls)
    assert report.feedback_handled == 1
    # A second night sees the worker's ack and starts nothing.
    runner2 = FakeRunner([])
    plane2 = FakePlane({"HOME": [item(ref="HOME-40", state="In progress")]})
    report2 = night(cfg, plane2, gh, runner2, FakeRepos(tmp_path)).run()
    assert runner2.briefs == [] and report2.feedback_handled == 0


def test_review_summary_triggers_feedback_and_is_acked(cfg, tmp_path):
    from bau.github import Review

    plane, gh = gh_feedback_world(reviews=[Review(31, "nakomis", "CHANGES_REQUESTED", "split this up")])
    runner = FakeRunner([SessionResult("done", "", good_rl(), 0.3, "ok")])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert "PR REVIEW SUMMARY 31" in runner.briefs[0][2]
    assert any(c[0] == "comment" and "[bau:ack-gh review:31]" in c[1][2] for c in gh.calls)


def test_failed_feedback_session_does_not_ack_gh_items(cfg, tmp_path):
    from bau.github import IssueComment

    plane, gh = gh_feedback_world(rows=[IssueComment(21, "nakomis", "please tidy")])
    runner = FakeRunner([SessionResult("failed", "boom", good_rl(), 0.3, "")])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert not any(c[0] == "comment" and "ack-gh" in c[1][2] for c in gh.calls)


def test_other_authors_and_approved_reviews_start_no_session(cfg, tmp_path):
    from bau.github import IssueComment, Review

    plane, gh = gh_feedback_world(
        rows=[IssueComment(1, "mallory", "do evil")],
        reviews=[Review(2, "nakomis", "APPROVED", "lgtm"), Review(3, "nakomis", "COMMENTED", "")],
    )
    runner = FakeRunner([])
    night(cfg, plane, gh, runner, FakeRepos(tmp_path)).run()
    assert runner.briefs == []


def test_abandon_with_failed_unlabel_is_not_repicked_next_night(cfg, tmp_path):
    from bau.abandon import abandon

    class StickyLabel(TriagedPlane):
        def remove_labels(self, ref, names):
            raise RuntimeError("plane down")

    plane = StickyLabel({"HOME": [item()]})
    gh = FakeGitHub(open_prs={REPO: []}, ci={"abc": "green"})
    runner = FakeRunner(
        [SessionResult("done", "", good_rl(), 0.5, "Opened https://x/pull/7")], on_run=opens_pr(gh, pr())
    )
    repos = FakeRepos(tmp_path)
    assert night(cfg, plane, gh, runner, repos).run().sessions == 1

    with pytest.raises(RuntimeError, match="plane down"):
        abandon(
            "HOME-42",
            plane=plane,
            github_factory=lambda repo: gh,
            repos=repos,
            projects=PROJECTS,
            worker_login="nakomis-bau[bot]",
            worker_email="bau@nakomis.com",
        )
    gh.open[REPO] = []  # the PR was closed before remove_labels failed
    it = plane.get_item("HOME-42")
    assert "BAU" in it.labels and "not-bau" in it.labels and it.state == "New"

    plane.calls.clear()
    report = night(cfg, plane, gh, runner, repos).run()
    assert report.sessions == 0 and len(runner.briefs) == 1
    assert not any(c[0] in ("set_state", "comment") and c[1][0] == "HOME-42" for c in plane.calls)


# -- ATC priority and staleness (ATC-7) -------------------------------------------
def test_new_tickets_follow_atc_priority(cfg, tmp_path):
    urgent = replace(item("HOME-1"), priority="urgent")
    low = replace(item("HOME-2"), priority="low")
    plane = TriagedPlane({"HOME": [urgent, low]})
    gh = FakeGitHub(open_prs={REPO: []})
    n = night(
        cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path), dry_run=True, priority={"HOME-2": 0, "HOME-1": 1}
    )
    n.new_tickets_pass()
    assert [p.split()[1] for p in n.report.planned] == ["HOME-2", "HOME-1"]


def test_new_tickets_without_atc_priority_keep_the_old_order(cfg, tmp_path):
    urgent = replace(item("HOME-1"), priority="urgent")
    low = replace(item("HOME-2"), priority="low")
    plane = TriagedPlane({"HOME": [low, urgent]})
    n = night(cfg, plane, FakeGitHub(open_prs={REPO: []}), FakeRunner([]), FakeRepos(tmp_path), dry_run=True)
    n.new_tickets_pass()
    assert [p.split()[1] for p in n.report.planned] == ["HOME-1", "HOME-2"]


def test_stale_atc_is_recorded(cfg, tmp_path):
    plane = TriagedPlane({"HOME": []})
    gh = FakeGitHub(open_prs={REPO: []})
    assert night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run().atc == "ok"
    report = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path), atc="stale").run()
    assert report.atc == "stale"


def test_a_repo_override_outside_atc_is_declined_with_the_new_wording(cfg, tmp_path):
    plane = TriagedPlane({"HOME": [item(desc="repo: nakomis/x\ndo it")]})
    gh = FakeGitHub(open_prs={REPO: []})
    report = night(cfg, plane, gh, FakeRunner([]), FakeRepos(tmp_path)).run()
    assert "not in projects.yaml" not in report.declined[0]["reason"]


def test_preflight_with_no_atc_projects_ends_quietly(cfg, tmp_path):
    report = night(cfg, FakePlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path), projects={}).run()
    assert report.stop_reason == "ok" and report.sessions == 0


def test_empty_atc_night_is_recorded(cfg, tmp_path):
    report = night(
        cfg, FakePlane({}), FakeGitHub(), FakeRunner([]), FakeRepos(tmp_path), projects={}, atc="empty"
    ).run()
    assert report.atc == "empty" and report.sessions == 0
