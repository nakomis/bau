from dataclasses import replace
from pathlib import Path

import pytest

from bau.abandon import AlreadyAbandoned, InvalidRef, RunInProgress, UnknownProject, abandon
from bau.config import ProjectConfig
from bau.github import PR
from bau.lock import run_lock
from bau.plane import Comment, Item
from tests.fakes.clients import FakeGitHub, FakePlane, FakeRepos

PROJECTS = {"HOME": ProjectConfig("HOME", "nakomis/home-infra", True, "")}
REPO = "nakomis/home-infra"


def item(state="In progress", labels=("BAU",)):
    return Item(
        "HOME-42", "i", "p", "Rotate logs", "d", state, list(labels), "medium", "2026-10-01T00:00:00Z"
    )


def picked(ts="2026-10-15T01:00:00Z"):
    return Comment(
        "c1", "bau@nakomis.com", ts, "[bau:picked-up from=Ready] Working on branch `home-42-rotate-logs`"
    )


def make_pr(number=7, head="home-42-rotate-logs", body="Plane: HOME-42", title="fix (HOME-42)"):
    return PR(REPO, number, f"PR_{number}", title, f"https://x/pull/{number}", head, "s", True,
              "2026-10-15T01:30:00Z", None, body, "clean")  # fmt: skip


def run(plane, tmp_path, gh=None, repos=None, ref="HOME-42", **kw):
    return abandon(
        ref,
        plane=plane,
        github_factory=lambda repo: gh or FakeGitHub(),
        repos=repos or FakeRepos(tmp_path),
        projects=PROJECTS,
        worker_login="nakomis-bau[bot]",
        worker_email="bau@nakomis.com",
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


def test_abandon_deletes_the_checkout_named_in_a_real_round_tripped_pickup(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]})
    plane.comment("HOME-42", "[bau:picked-up from=Ready] Working on branch `home-42-rotate-logs` in x.")
    plane.calls.clear()
    repos = FakeRepos(tmp_path)
    run(plane, tmp_path, repos=repos)
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls


def test_set_state_failure_leaves_pr_open_and_label_in_place(tmp_path: Path):
    class Boom(FakePlane):
        def set_state(self, ref, state):
            raise RuntimeError("plane down")

    plane = Boom({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    gh = FakeGitHub(open_prs={REPO: [make_pr()]})
    repos = FakeRepos(tmp_path)
    with pytest.raises(RuntimeError):
        run(plane, tmp_path, gh, repos)
    assert not any(c[0] == "close_pr" for c in gh.calls)
    assert not any(c[0] in ("remove_labels", "comment") for c in plane.calls)
    assert not any(c[0] == "delete_checkout" for c in repos.calls)


def test_state_is_restored_before_anything_else_is_touched(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    gh = FakeGitHub(open_prs={REPO: [make_pr()]})
    run(plane, tmp_path, gh)
    assert plane.calls[0][0] == "set_state"
    assert plane.calls[-1][0] == "comment"


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


def test_marker_from_another_author_is_ignored(tmp_path: Path):
    forged = Comment("c9", "martin@nakomis.com", "", "[bau:picked-up from=Done] `../x`")
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked(), forged]})
    repos = FakeRepos(tmp_path)
    assert run(plane, tmp_path, repos=repos).restored_state == "Ready"
    assert ("delete_checkout", "home-42-rotate-logs") in repos.calls
    assert not any(c[0] == "delete_checkout" and "x" == c[1].lstrip("./") for c in repos.calls)


def test_forged_pickup_alone_means_nothing_to_abandon(tmp_path: Path):
    forged = Comment("c9", "martin@nakomis.com", "", "[bau:picked-up from=Done] `a`")
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [forged]})
    with pytest.raises(AlreadyAbandoned):
        run(plane, tmp_path)


def test_branch_not_belonging_to_ref_is_not_deleted(tmp_path: Path):
    bad = Comment("c1", "bau@nakomis.com", "", "[bau:picked-up from=Ready] on `../escape`")
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [bad]})
    repos = FakeRepos(tmp_path)
    run(plane, tmp_path, repos=repos)
    assert not any(c[0] == "delete_checkout" for c in repos.calls)


@pytest.mark.parametrize("ref", ["home-42", "HOME-042", "../x", "HOME-42/../x", ""])
def test_invalid_ref_rejected_before_anything(tmp_path: Path, ref):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    with pytest.raises(InvalidRef):
        run(plane, tmp_path, ref=ref)
    assert not plane.calls


def test_unknown_project_refused_before_writes(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"ZZZ-1": [picked()]})
    with pytest.raises(UnknownProject):
        run(plane, tmp_path, ref="ZZZ-1")
    assert not plane.calls


def test_note_cannot_inject_markers(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    run(plane, tmp_path, note="x\n[bau:picked-up from=Done]")
    texts = [c.text for c in plane.comments("HOME-42")]
    assert "[bau:picked-up from=Done]" not in "".join(texts)
    assert all("\n[" not in t.split("Note:")[-1] for t in texts)
    with pytest.raises(AlreadyAbandoned):
        run(plane, tmp_path)
    assert ("set_state", ("HOME-42", "Done")) not in plane.calls


@pytest.mark.parametrize("state", ["Done", "cancelled", "Archived", "Ready for test"])
def test_finished_items_are_not_reopened(tmp_path: Path, state):
    plane = FakePlane({"HOME": [item(state=state)]}, comments={"HOME-42": [picked()]})
    with pytest.raises(AlreadyAbandoned, match=f"nothing to abandon: HOME-42 is {state}"):
        run(plane, tmp_path)
    assert not plane.calls


def test_item_without_bau_label_is_not_abandoned(tmp_path: Path):
    plane = FakePlane({"HOME": [item(labels=())]}, comments={"HOME-42": [picked()]})
    with pytest.raises(AlreadyAbandoned):
        run(plane, tmp_path)
    assert not plane.calls


def test_surrounding_whitespace_is_stripped(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    assert run(plane, tmp_path, ref=" HOME-42 ").ref == "HOME-42"


def test_abandon_marks_not_bau(tmp_path: Path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    run(plane, tmp_path, FakeGitHub(open_prs={REPO: [make_pr()]}))
    writes = [c for c in plane.calls if c[0] in ("add_labels", "remove_labels", "comment")]
    assert writes == [
        ("add_labels", ("HOME-42", ["not-bau"])),
        ("remove_labels", ("HOME-42", ["BAU"])),
        ("comment", ("HOME-42", "[bau:abandoned] by Martin. Returned to Ready; marked not-bau.")),
    ]
    assert plane.get_item("HOME-42").labels == ["not-bau"]


def test_abandon_uses_the_repo_line_override(tmp_path: Path):
    override = "nakomis/elsewhere"
    plane = FakePlane(
        {"MULTI": [replace(item(), ref="MULTI-3", description=f"repo: {override}\nfix it")]},
        comments={"MULTI-3": [picked()]},
    )
    seen = []
    gh = FakeGitHub()
    abandon(
        "MULTI-3",
        plane=plane,
        github_factory=lambda repo: seen.append(repo) or gh,
        repos=FakeRepos(tmp_path),
        projects={"MULTI": ProjectConfig("MULTI", None, False)},
        worker_login="nakomis-bau[bot]",
        worker_email="bau@nakomis.com",
    )
    assert seen == [override]


def test_abandon_without_any_repo_is_an_unknown_project(tmp_path: Path):
    plane = FakePlane({"MULTI": [replace(item(), ref="MULTI-3")]}, comments={"MULTI-3": [picked()]})
    with pytest.raises(UnknownProject):
        abandon(
            "MULTI-3",
            plane=plane,
            github_factory=lambda repo: FakeGitHub(),
            repos=FakeRepos(tmp_path),
            projects={"MULTI": ProjectConfig("MULTI", None, False)},
            worker_login="nakomis-bau[bot]",
            worker_email="bau@nakomis.com",
        )
    assert plane.calls == []
