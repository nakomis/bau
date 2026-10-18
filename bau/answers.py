from __future__ import annotations

import logging
from pathlib import Path

from bau.abandon import REF_PATTERN, RUN_IN_PROGRESS, InvalidRef, RunInProgress, UnknownProject, clean_note
from bau.config import ProjectConfig
from bau.lock import run_lock
from bau.markers import Answer, answers_comment, latest_asked
from bau.plane import PlaneClient, split_ref

log = logging.getLogger(__name__)
WAITING = "bau-questions"


class NotWaiting(RuntimeError):
    pass


class StaleRound(RuntimeError):
    pass


class MissingAnswer(ValueError):
    pass


def clean_answer(text: str) -> str:
    """Keep Martin's line breaks, but no bracket characters: an answer must never forge a [bau:...] marker."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip().replace("[", "(").replace("]", ")")


def _check_ref(ref: str, projects: dict[str, ProjectConfig]) -> str:
    ref = ref.strip()
    if not REF_PATTERN.match(ref):
        raise InvalidRef("invalid ref")
    if split_ref(ref)[0] not in projects:
        raise UnknownProject("unknown project")
    return ref


def _locked(lock_path: Path | None, fn):
    if lock_path is None:
        return fn()
    with run_lock(lock_path) as held:
        if not held:
            raise RunInProgress(RUN_IN_PROGRESS)
        return fn()


def submit_answers(
    ref: str,
    round: int,
    asked_at: str,
    answers: list[Answer],
    *,
    plane: PlaneClient,
    projects: dict[str, ProjectConfig],
    worker_email: str,
    lock_path: Path | None,
) -> None:
    ref = _check_ref(ref, projects)

    def go() -> None:
        item = plane.get_item(ref)
        if WAITING not in item.labels:
            raise NotWaiting(f"{ref} is not waiting for answers")
        asked = latest_asked(plane.comments(ref), worker_email)
        if asked is None:
            raise NotWaiting(f"{ref} has no questions from BAU")
        # The same round number can be asked again after a description edit, so the timestamp counts too.
        if asked.round != round or asked.at != asked_at:
            raise StaleRound(f"the questions for {ref} have changed; reload and answer again")
        by_id = {a.id: a for a in answers}
        if len(by_id) != len(answers) or set(by_id) != {q.id for q in asked.questions}:
            raise MissingAnswer("every question needs exactly one answer")
        cleaned = []
        for q in asked.questions:
            a = by_id[q.id]
            text = clean_answer(a.text)
            if not text:
                raise MissingAnswer(f"question {q.id} has no answer")
            if a.accepted and text != clean_answer(q.recommendation):
                raise MissingAnswer(f"question {q.id} accepts a recommendation it was not given")
            cleaned.append(Answer(q.id, a.accepted, text))
        plane.comment(ref, answers_comment(asked.round, asked.questions, cleaned))
        plane.remove_labels(ref, [WAITING])

    _locked(lock_path, go)


def mark_not_bau(
    ref: str,
    note: str,
    *,
    plane: PlaneClient,
    projects: dict[str, ProjectConfig],
    lock_path: Path | None,
) -> None:
    ref = _check_ref(ref, projects)
    note = clean_note(note)

    def go() -> None:
        # not-bau, then the audit comment, then BAU comes off: a failure part-way never leaves a relabel untraced.
        plane.add_labels(ref, ["not-bau"])
        suffix = f" Note: {note}" if note else ""
        plane.comment(ref, "[bau] Marked not-bau by Martin from the BAU dashboard." + suffix)
        plane.remove_labels(ref, ["BAU", WAITING])

    _locked(lock_path, go)
