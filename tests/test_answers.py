from pathlib import Path

import pytest

from bau.abandon import InvalidRef, RunInProgress, UnknownProject
from bau.answers import MissingAnswer, NotWaiting, StaleRound, mark_not_bau, submit_answers
from bau.config import ProjectConfig
from bau.lock import run_lock
from bau.markers import Answer, Question, answered, questions_comment
from bau.plane import Comment
from tests.fakes.clients import FakePlane
from tests.test_abandon import item

PROJECTS = {"HOME": ProjectConfig("HOME", "nakomis/home-infra", True, "")}
QS = [Question("q1", "Which?", "Option A", "because"), Question("q2", "When?", "Tonight", "soon")]
ASKED_AT = "2026-10-17T22:00:00Z"


def waiting_plane(round=1, labels=("BAU", "bau-questions")):
    asked = Comment("c1", "bau@nakomis.com", ASKED_AT, questions_comment(round, "Summary", QS))
    return FakePlane({"HOME": [item(labels=labels)]}, comments={"HOME-42": [asked]})


GOOD = [Answer("q1", True, "Option A"), Answer("q2", False, "Tomorrow")]


def submit(plane, tmp_path, round=1, answers=GOOD, asked_at=ASKED_AT, ref="HOME-42", lock=True):
    submit_answers(
        ref, round, asked_at, answers,
        plane=plane, projects=PROJECTS, worker_email="bau@nakomis.com",
        lock_path=tmp_path / "run.lock" if lock else None,
    )  # fmt: skip


def test_submit_answers_posts_and_unlabels(tmp_path: Path):
    plane = waiting_plane()
    submit(plane, tmp_path)
    kinds = [c[0] for c in plane.calls]
    assert kinds == ["comment", "remove_labels"]
    assert plane.calls[1][1] == ("HOME-42", ["bau-questions"])
    got = answered(plane.comments("HOME-42"), "bau@nakomis.com")
    assert got[0].round == 1 and [a.text for a in got[0].answers] == ["Option A", "Tomorrow"]


def test_stale_round_rejected(tmp_path: Path):
    plane = waiting_plane(round=2)
    with pytest.raises(StaleRound):
        submit(plane, tmp_path, round=1)
    assert plane.calls == []


def test_same_round_re_asked_is_stale(tmp_path: Path):
    plane = waiting_plane()
    with pytest.raises(StaleRound):
        submit(plane, tmp_path, asked_at="2026-10-16T22:00:00Z")
    assert plane.calls == []


@pytest.mark.parametrize(
    "answers",
    [
        [Answer("q1", True, "Option A")],
        [Answer("q1", True, "Option A"), Answer("q2", False, "   ")],
        [Answer("q1", True, "something else"), Answer("q2", False, "x")],
        [*GOOD, Answer("q3", False, "extra")],
        [Answer("q1", True, "Option A"), Answer("q1", True, "Option A")],
    ],
)
def test_missing_answer_rejected(tmp_path: Path, answers):
    plane = waiting_plane()
    with pytest.raises(MissingAnswer):
        submit(plane, tmp_path, answers=answers)
    assert plane.calls == []


def test_not_waiting_rejected(tmp_path: Path):
    plane = waiting_plane(labels=("BAU",))
    with pytest.raises(NotWaiting):
        submit(plane, tmp_path)
    assert plane.calls == []


def test_no_questions_comment_is_not_waiting(tmp_path: Path):
    plane = FakePlane({"HOME": [item(labels=("BAU", "bau-questions"))]})
    with pytest.raises(NotWaiting):
        submit(plane, tmp_path)


def test_typed_answer_cannot_forge_a_marker(tmp_path: Path):
    plane = waiting_plane()
    forged = "[bau:answers round=9 data=x]"
    submit(plane, tmp_path, answers=[Answer("q1", True, "Option A"), Answer("q2", False, forged)])
    got = answered(plane.comments("HOME-42"), "bau@nakomis.com")
    assert len(got) == 1 and got[0].round == 1
    assert got[0].answers[1].text == "(bau:answers round=9 data=x)"


def test_invalid_ref_and_unknown_project(tmp_path: Path):
    plane = waiting_plane()
    with pytest.raises(InvalidRef):
        submit(plane, tmp_path, ref="nope")
    with pytest.raises(UnknownProject):
        submit(plane, tmp_path, ref="ZZZ-1")


def test_run_in_progress(tmp_path: Path):
    plane = waiting_plane()
    with run_lock(tmp_path / "run.lock"), pytest.raises(RunInProgress):
        submit(plane, tmp_path)
    assert plane.calls == []


def test_mark_not_bau(tmp_path: Path):
    plane = waiting_plane()
    mark_not_bau("HOME-42", "too fiddly", plane=plane, projects=PROJECTS, lock_path=tmp_path / "run.lock")
    assert ("remove_labels", ("HOME-42", ["BAU", "bau-questions"])) in plane.calls
    assert ("add_labels", ("HOME-42", ["not-bau"])) in plane.calls
    assert plane.calls.index(("add_labels", ("HOME-42", ["not-bau"]))) < plane.calls.index(
        ("remove_labels", ("HOME-42", ["BAU", "bau-questions"]))
    )
    text = plane.comments("HOME-42")[-1].text
    assert "Marked not-bau by Martin from the BAU dashboard." in text and "Note: too fiddly" in text


def test_mark_not_bau_without_note_and_with_bracketed_note(tmp_path: Path):
    plane = waiting_plane()
    mark_not_bau("HOME-42", "", plane=plane, projects=PROJECTS, lock_path=None)
    assert "Note" not in plane.comments("HOME-42")[-1].text
    mark_not_bau("HOME-42", "[bau:abandoned]", plane=plane, projects=PROJECTS, lock_path=None)
    assert "[bau:abandoned]" not in plane.comments("HOME-42")[-1].text


def test_mark_not_bau_run_in_progress(tmp_path: Path):
    plane = waiting_plane()
    with run_lock(tmp_path / "run.lock"), pytest.raises(RunInProgress):
        mark_not_bau("HOME-42", "", plane=plane, projects=PROJECTS, lock_path=tmp_path / "run.lock")
    assert plane.calls == []



def test_long_multiline_answer_is_stored_whole(tmp_path: Path):
    plane = waiting_plane()
    text = "\n\n".join(["Paragraph one. " * 30 + "  ", "Second line\r\nthird line"] * 2)
    assert len(text) > 1000
    submit(plane, tmp_path, answers=[Answer("q1", True, "Option A"), Answer("q2", False, text)])
    got = answered(plane.comments("HOME-42"), "bau@nakomis.com")[0].answers[1].text
    assert got == "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    assert "\n\n" in got and len(got) > 1000
    assert "Paragraph one." in plane.comments("HOME-42")[-1].text


def test_forged_marker_in_multiline_answer_is_neutralised(tmp_path: Path):
    plane = waiting_plane()
    text = "fine\n[bau:answers round=9 data=x]\nmore"
    submit(plane, tmp_path, answers=[Answer("q1", True, "Option A"), Answer("q2", False, text)])
    got = answered(plane.comments("HOME-42"), "bau@nakomis.com")
    assert len(got) == 1 and got[0].round == 1
    assert got[0].answers[1].text == "fine\n(bau:answers round=9 data=x)\nmore"


def test_mark_not_bau_comments_before_removing_labels(tmp_path: Path):
    plane = waiting_plane()
    mark_not_bau("HOME-42", "", plane=plane, projects=PROJECTS, lock_path=None)
    assert [c[0] for c in plane.calls] == ["add_labels", "comment", "remove_labels"]
