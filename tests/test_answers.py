from pathlib import Path

import pytest

from bau.abandon import InvalidRef, RunInProgress, UnknownProject
from bau.answers import MissingAnswer, NotWaiting, StaleRound, mark_not_bau, submit_answers
from bau.config import ProjectConfig
from bau.lock import run_lock
from bau.markers import Answer, Question, answered, questions_comment
from bau.purr import Comment
from tests.fakes.clients import FakePurr
from tests.test_abandon import item

PROJECTS = {"HOME": ProjectConfig("HOME", "nakomis/home-infra", True, "")}
QS = [Question("q1", "Which?", "Option A", "because"), Question("q2", "When?", "Tonight", "soon")]
ASKED_AT = "2026-10-17T22:00:00Z"


def waiting_purr(round=1, labels=("BAU", "bau-questions")):
    asked = Comment("c1", "bau", ASKED_AT, questions_comment(round, "Summary", QS))
    return FakePurr({"HOME": [item(labels=labels)]}, comments={"HOME-42": [asked]})


GOOD = [Answer("q1", True, "Option A"), Answer("q2", False, "Tomorrow")]


def submit(purr, tmp_path, round=1, answers=GOOD, asked_at=ASKED_AT, ref="HOME-42", lock=True):
    submit_answers(
        ref, round, asked_at, answers,
        purr=purr, projects=PROJECTS, worker_actor="bau",
        lock_path=tmp_path / "run.lock" if lock else None,
    )  # fmt: skip


def test_submit_answers_posts_and_unlabels(tmp_path: Path):
    purr = waiting_purr()
    submit(purr, tmp_path)
    kinds = [c[0] for c in purr.calls]
    assert kinds == ["comment", "remove_labels"]
    assert purr.calls[1][1] == ("HOME-42", ["bau-questions"])
    got = answered(purr.comments("HOME-42"), "bau")
    assert got[0].round == 1 and [a.text for a in got[0].answers] == ["Option A", "Tomorrow"]


def test_stale_round_rejected(tmp_path: Path):
    purr = waiting_purr(round=2)
    with pytest.raises(StaleRound):
        submit(purr, tmp_path, round=1)
    assert purr.calls == []


def test_same_round_re_asked_is_stale(tmp_path: Path):
    purr = waiting_purr()
    with pytest.raises(StaleRound):
        submit(purr, tmp_path, asked_at="2026-10-16T22:00:00Z")
    assert purr.calls == []


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
    purr = waiting_purr()
    with pytest.raises(MissingAnswer):
        submit(purr, tmp_path, answers=answers)
    assert purr.calls == []


def test_not_waiting_rejected(tmp_path: Path):
    purr = waiting_purr(labels=("BAU",))
    with pytest.raises(NotWaiting):
        submit(purr, tmp_path)
    assert purr.calls == []


def test_no_questions_comment_is_not_waiting(tmp_path: Path):
    purr = FakePurr({"HOME": [item(labels=("BAU", "bau-questions"))]})
    with pytest.raises(NotWaiting):
        submit(purr, tmp_path)


def test_typed_answer_cannot_forge_a_marker(tmp_path: Path):
    purr = waiting_purr()
    forged = "[bau:answers round=9 data=x]"
    submit(purr, tmp_path, answers=[Answer("q1", True, "Option A"), Answer("q2", False, forged)])
    got = answered(purr.comments("HOME-42"), "bau")
    assert len(got) == 1 and got[0].round == 1
    assert got[0].answers[1].text == "(bau:answers round=9 data=x)"


def test_invalid_ref_and_unknown_project(tmp_path: Path):
    purr = waiting_purr()
    with pytest.raises(InvalidRef):
        submit(purr, tmp_path, ref="nope")
    with pytest.raises(UnknownProject):
        submit(purr, tmp_path, ref="ZZZ-1")


def test_run_in_progress(tmp_path: Path):
    purr = waiting_purr()
    with run_lock(tmp_path / "run.lock"), pytest.raises(RunInProgress):
        submit(purr, tmp_path)
    assert purr.calls == []


def test_mark_not_bau(tmp_path: Path):
    purr = waiting_purr()
    mark_not_bau("HOME-42", "too fiddly", purr=purr, projects=PROJECTS, lock_path=tmp_path / "run.lock")
    assert ("remove_labels", ("HOME-42", ["BAU", "bau-questions"])) in purr.calls
    assert ("add_labels", ("HOME-42", ["not-bau"])) in purr.calls
    assert purr.calls.index(("add_labels", ("HOME-42", ["not-bau"]))) < purr.calls.index(
        ("remove_labels", ("HOME-42", ["BAU", "bau-questions"]))
    )
    text = purr.comments("HOME-42")[-1].text
    assert "Marked not-bau by Martin from the BAU dashboard." in text and "Note: too fiddly" in text


def test_mark_not_bau_without_note_and_with_bracketed_note(tmp_path: Path):
    purr = waiting_purr()
    mark_not_bau("HOME-42", "", purr=purr, projects=PROJECTS, lock_path=None)
    assert "Note" not in purr.comments("HOME-42")[-1].text
    mark_not_bau("HOME-42", "[bau:abandoned]", purr=purr, projects=PROJECTS, lock_path=None)
    assert "[bau:abandoned]" not in purr.comments("HOME-42")[-1].text


def test_mark_not_bau_run_in_progress(tmp_path: Path):
    purr = waiting_purr()
    with run_lock(tmp_path / "run.lock"), pytest.raises(RunInProgress):
        mark_not_bau("HOME-42", "", purr=purr, projects=PROJECTS, lock_path=tmp_path / "run.lock")
    assert purr.calls == []


def test_long_multiline_answer_is_stored_whole(tmp_path: Path):
    purr = waiting_purr()
    text = "\n\n".join(["Paragraph one. " * 50 + "  ", "Second line\r\nthird line"] * 2)
    assert len(text) > 1500
    submit(purr, tmp_path, answers=[Answer("q1", True, "Option A"), Answer("q2", False, text)])
    got = answered(purr.comments("HOME-42"), "bau")[0].answers[1].text
    assert got == "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    assert "\n\n" in got and len(got) > 1500
    assert "Paragraph one." in purr.comments("HOME-42")[-1].text


def test_forged_marker_in_multiline_answer_is_neutralised(tmp_path: Path):
    purr = waiting_purr()
    text = "fine\n[bau:answers round=9 data=x]\nmore"
    submit(purr, tmp_path, answers=[Answer("q1", True, "Option A"), Answer("q2", False, text)])
    got = answered(purr.comments("HOME-42"), "bau")
    assert len(got) == 1 and got[0].round == 1
    assert got[0].answers[1].text == "fine\n(bau:answers round=9 data=x)\nmore"


def test_mark_not_bau_comments_before_removing_labels(tmp_path: Path):
    purr = waiting_purr()
    mark_not_bau("HOME-42", "", purr=purr, projects=PROJECTS, lock_path=None)
    assert [c[0] for c in purr.calls] == ["add_labels", "comment", "remove_labels"]
