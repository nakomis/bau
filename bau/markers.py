"""The Plane comment protocol: markers the worker writes and later reads back.

Only comments authored by the worker are ever parsed, so a marker pasted into
a ticket description or someone else's comment is inert.
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass

from bau.plane import Comment

VERDICTS = ("ready", "questions", "too_big")


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    recommendation: str
    why: str


@dataclass(frozen=True)
class Answer:
    id: str
    accepted: bool
    text: str


@dataclass(frozen=True)
class Verdict:
    verdict: str
    round: int
    desc: str
    at: str


@dataclass(frozen=True)
class Asked:
    round: int
    summary: str
    questions: list[Question]
    at: str


@dataclass(frozen=True)
class Answered:
    round: int
    answers: list[Answer]
    at: str


# Anchored: the triage marker must open the comment; the questions and answers data markers must be
# the whole of its last non-empty line. A marker quoted mid-comment (say, inside agent text) is inert.
_TRIAGE = re.compile(r"\[bau:triage verdict=(\w+) round=(\d+) desc=([0-9a-f]{12})\]")
_QUESTIONS = re.compile(r"\[bau:questions round=(\d+) data=([A-Za-z0-9_-]*)\]")
_ANSWERS = re.compile(r"\[bau:answers round=(\d+) data=([A-Za-z0-9_-]*)\]")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def desc_hash(description: str) -> str:
    normalised = " ".join(description.split())
    return hashlib.sha256(normalised.encode()).hexdigest()[:12]


def _encode(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(data: str) -> object | None:
    try:
        return json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
    except ValueError:  # binascii.Error and JSON/Unicode decode errors are all ValueErrors
        return None


def triage_comment(verdict: str, round: int, desc: str, reason_said: str) -> str:
    return f"[bau:triage verdict={verdict} round={round} desc={desc}]\n\n{reason_said}"


def questions_comment(round: int, summary: str, questions: list[Question]) -> str:
    paras = [f"{q.text}\nRecommended: {q.recommendation}\nWhy: {q.why}" for q in questions]
    payload = {
        "summary": summary,
        "questions": [
            {"id": q.id, "text": q.text, "recommendation": q.recommendation, "why": q.why} for q in questions
        ],
    }
    marker = f"[bau:questions round={round} data={_encode(payload)}]"
    return "\n\n".join([summary, *paras, marker])


def answers_comment(round: int, questions: list[Question], answers: list[Answer]) -> str:
    by_id = {a.id: a for a in answers}
    paras = ["Martin answered on bau.home.nakomis.com:"]
    for q in questions:
        a = by_id.get(q.id)
        if a is None:
            continue
        how = "recommendation accepted" if a.accepted else "Martin's own words"
        paras.append(f"{q.text}\n{a.text} ({how})")
    payload = {"answers": [{"id": a.id, "accepted": a.accepted, "text": a.text} for a in answers]}
    paras.append(f"[bau:answers round={round} data={_encode(payload)}]")
    return "\n\n".join(paras)


def swept_comment(verdict: str, reason_said: str) -> str:
    return f"[bau:swept verdict={verdict}]\n\n{reason_said}"


def _by_worker(comments: list[Comment], worker_email: str) -> list[Comment]:
    return [c for c in comments if c.author_email == worker_email]


def _parse_asked(c: Comment) -> Asked | None:
    m = _QUESTIONS.fullmatch(_last_line(c.text))
    if not m:
        return None
    data = _decode(m.group(2))
    try:
        qs = [
            Question(q["id"], q["text"], q["recommendation"], q["why"])
            for q in data["questions"]  # type: ignore[index]
        ]
        summary = data["summary"]  # type: ignore[index]
        if not isinstance(summary, str) or not all(
            isinstance(v, str) for q in qs for v in (q.id, q.text, q.recommendation, q.why)
        ):
            return None
        return Asked(int(m.group(1)), summary, qs, c.created_at)
    except (KeyError, TypeError):
        return None


def _parse_answered(c: Comment) -> Answered | None:
    m = _ANSWERS.fullmatch(_last_line(c.text))
    if not m:
        return None
    data = _decode(m.group(2))
    try:
        answers = [
            Answer(a["id"], a["accepted"], a["text"])
            for a in data["answers"]  # type: ignore[index]
        ]
        if not all(
            isinstance(a.id, str) and isinstance(a.accepted, bool) and isinstance(a.text, str)
            for a in answers
        ):
            return None
        return Answered(int(m.group(1)), answers, c.created_at)
    except (KeyError, TypeError):
        return None


def _all_asked(comments: list[Comment], worker_email: str) -> list[Asked]:
    parsed = (_parse_asked(c) for c in _by_worker(comments, worker_email))
    return [a for a in parsed if a]


def latest_verdict(comments: list[Comment], worker_email: str) -> Verdict | None:
    found = None
    for c in _by_worker(comments, worker_email):
        m = _TRIAGE.match(c.text.lstrip())
        if m and m.group(1) in VERDICTS:
            found = Verdict(m.group(1), int(m.group(2)), m.group(3), c.created_at)
    return found


def latest_asked(comments: list[Comment], worker_email: str) -> Asked | None:
    asked = _all_asked(comments, worker_email)
    return asked[-1] if asked else None


def answered(comments: list[Comment], worker_email: str) -> list[Answered]:
    parsed = (_parse_answered(c) for c in _by_worker(comments, worker_email))
    return [a for a in parsed if a]


def history(comments: list[Comment], worker_email: str) -> list[tuple[Asked, Answered | None]]:
    answers = {a.round: a for a in answered(comments, worker_email)}
    return [(a, answers.get(a.round)) for a in _all_asked(comments, worker_email)]
