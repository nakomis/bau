from pathlib import Path

import pytest

from bau.abandon import AlreadyAbandoned, RunInProgress, abandon
from bau.config import ProjectConfig
from bau.github import PR
from bau.lock import run_lock
from bau.plane import Comment, Item
from tests.fakes.clients import FakeGitHub, FakePlane, FakeRepos

PROJECTS = {"HOME": ProjectConfig("HOME", "nakomis/home-infra", True, "")}
REPO = "nakomis/home-infra"


def item(state="In progress"):
    return Item("HOME-42", "i", "p", "Rotate logs", "d", state, ["BAU"], "medium", "2026-10-01T00:00:00Z")


def picked(ts="2026-10-15T01:00:00Z"):
    return Comment(
        "c1", "bau@nakomis.com", ts, "[bau:picked-up from=Ready] Working on branch `home-42-rotate-logs`"
    )


def make_pr(number=7, head="home-42-rotate-logs", body="Plane: HOME-42", title="fix (HOME-42)"):
    return PR(REPO, number, f"PR_{number}", title, f"https://x/pull/{number}", head, "s", True,
              "2026-10-15T01:30:00Z", None, body, "clean")  # fmt: skip


def run(plane, tmp_path, gh=None, repos=None, **kw):
    return abandon(
        "HOME-42",
        plane=plane,
        github_factory=lambda repo: gh or FakeGitHub(),
        repos=repos or FakeRepos(tmp_path),
        projects=PROJECTS,
        worker_login="nakomis-bau[bot]",
        lock_path=tmp_path / "run.lock",
        **kw,
    )


def test_abandon_closes_pr_restores_state_and_unlabels(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    gh = FakeGitHub(open_prs={REPO: [make_pr()]})
    repos = FakeRepos(tmp_path)
    r = run(plane, tmp_path, gh, repos, note="wrong approach")
    assert r.pr == 7 and r.restored_state == "Ready"
    assert ("close_pr", (REPO, 7)) in gh.calls
    assert ("set_state", ("HOME-42", "Ready")) in plane.calls
    assert ("remove_labels", ("HOME-42", ["BAU"])) in plane.calls
    assert any(
        c[0] == "comment" and "[bau:abandoned]" in c[1][1] and "wrong approach" in c[1][1]
        for c in plane.calls
    )
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls


def test_abandon_without_open_pr_still_restores(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    repos = FakeRepos(tmp_path)
    r = run(plane, tmp_path, repos=repos)
    assert r.pr is None and r.restored_state == "Ready"
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls


def test_pr_with_mismatched_branch_is_left_alone(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    gh = FakeGitHub(open_prs={REPO: [make_pr(head="feature-x")]})
    r = run(plane, tmp_path, gh)
    assert r.pr is None
    assert not any(c[0] == "close_pr" for c in gh.calls)


def test_abandon_twice_is_rejected(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    run(plane, tmp_path)
    with pytest.raises(AlreadyAbandoned):
        run(plane, tmp_path)


def test_order_not_timestamp_decides(tmp_path: Path):
    # The abandon comment comes later in the list but carries an earlier-looking timestamp string.
    comments = [
        picked("2026-10-16T09:00:00Z"),
        Comment("c2", "bau@nakomis.com", "", "[bau:abandoned] by Martin"),
    ]
    plane = FakePlane({"HOME": [item(state="Ready")]}, comments={"HOME-42": comments})
    with pytest.raises(AlreadyAbandoned):
        run(plane, tmp_path)


def test_repick_after_abandon_can_be_abandoned_again(tmp_path: Path):
    comments = [
        picked(),
        Comment("c2", "bau@nakomis.com", "", "[bau:abandoned] by Martin"),
        Comment("c3", "bau@nakomis.com", "", "[bau:picked-up from=New] Working on branch `home-42-x`"),
    ]
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": comments})
    assert run(plane, tmp_path).restored_state == "New"


def test_no_pickup_is_rejected(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": []})
    with pytest.raises(AlreadyAbandoned, match="no active BAU pick-up"):
        run(plane, tmp_path)


def test_lock_held_raises_run_in_progress(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    with run_lock(tmp_path / "run.lock") as held:
        assert held
        with pytest.raises(RunInProgress):
            run(plane, tmp_path)
    assert not plane.calls
