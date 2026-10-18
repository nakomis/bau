"""Nightly triage: one read-only session per BAU item, deciding ready, questions or too big.

The verdict lives in Plane as a worker-authored marker comment; Cere stores nothing. "Newer" always
means later in the item's comment list (Plane returns comments oldest first), not a timestamp compare.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bau.markers import (
    TRIAGE_FAILED,
    VERDICTS,
    Question,
    Verdict,
    answered,
    desc_hash,
    latest_verdict,
    questions_comment,
    triage_comment,
    triage_failed_comment,
)
from bau.plane import Comment, Item
from bau.prompts import build_triage_brief
from bau.sweep import JSON_BLOCK, _clean_reason, _said

if TYPE_CHECKING:
    from bau.runloop import Night

NEVER_TRIAGE = ("not-bau", "blocked")
MAX_QUESTIONS = 5
MAX_FIELD_CHARS = 500  # stated in TRIAGE_PROMPT; longer text is rejected, never cut short
QUESTION_FIELDS = ("id", "text", "recommendation", "why")
SPEAKER = "triage agent"
TRIAGE_GAVE_UP = "triage failed twice; comment on the ticket to retry"


@dataclass(frozen=True)
class TriageResult:
    verdict: str
    reason: str
    summary: str
    questions: list[Question]


def current_round(comments: list[Comment], worker_email: str) -> int:
    """Distinct answered rounds, so a double-submitted answers comment cannot skip a round."""
    return len({a.round for a in answered(comments, worker_email)}) + 1


def _verdict_and_later_answers(comments: list[Comment], worker_email: str) -> tuple[Verdict | None, bool]:
    """The latest verdict, and whether an answers comment comes after it."""
    found, verdict_at, answers_at = None, -1, -1
    for i, c in enumerate(comments):
        v = latest_verdict([c], worker_email)
        if v:
            found, verdict_at = v, i
        if answered([c], worker_email):
            answers_at = i
    return found, answers_at > verdict_at


def triage_failures(comments: list[Comment], owner_email: str, worker_email: str) -> int:
    """The worker's triage-failed notes since the last verdict or the owner's last comment, whichever
    is later: a verdict or a word from Martin starts the count again."""
    count = 0
    for c in comments:
        if c.author_email == owner_email or latest_verdict([c], worker_email):
            count = 0
        elif c.author_email == worker_email and c.text.lstrip().startswith(TRIAGE_FAILED):
            count += 1
    return count


def needs_triage(item: Item, comments: list[Comment], worker_email: str) -> bool:
    if "BAU" not in item.labels or any(label in NEVER_TRIAGE for label in item.labels):
        return False
    if "bau-questions" in item.labels:
        # Waiting for Martin, unless the description changed under the questions: ask afresh.
        verdict = latest_verdict(comments, worker_email)
        return (
            verdict is not None
            and verdict.verdict == "questions"
            and verdict.desc != desc_hash(item.description)
        )
    # Only a ready verdict for this description, with nothing answered since, is final. Any other
    # verdict on an item that is plain BAU means a label was changed by hand: triage it again.
    return not ready_for_pickup(item, comments, worker_email)


def ready_for_pickup(item: Item, comments: list[Comment], worker_email: str) -> bool:
    verdict, answered_since = _verdict_and_later_answers(comments, worker_email)
    return (
        verdict is not None
        and verdict.verdict == "ready"
        and verdict.desc == desc_hash(item.description)
        and not answered_since
    )


def _text(value: object) -> str | None:
    """The reason made safe for Plane, or None when it is not a non-blank string."""
    if not isinstance(value, str):
        return None
    cleaned = _clean_reason(value)
    return cleaned if cleaned.strip() else None


def _field(value: object) -> str | None:
    """A summary or question field made safe for Plane; None when not a string or over the limit.
    Cleaning only ever shortens text, so the length check on the raw text bounds the cleaned text."""
    if not isinstance(value, str) or len(" ".join(value.split())) > MAX_FIELD_CHARS:
        return None
    return _clean_reason(value, MAX_FIELD_CHARS)


def _question(raw: object) -> Question | None:
    if not isinstance(raw, dict):
        return None
    fields = [_field(raw.get(k)) for k in QUESTION_FIELDS]
    if any(not f for f in fields):
        return None
    return Question(*fields)  # type: ignore[arg-type]


def parse_triage(text: str) -> TriageResult | None:
    """The final fenced JSON block, held to the contract; anything else is None (nothing is written)."""
    blocks = JSON_BLOCK.findall(text or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return None
    verdict = data["verdict"]
    reason = _text(data.get("reason"))
    summary = _field(data.get("summary", ""))
    if reason is None or summary is None:
        return None
    raw_questions = data.get("questions")
    if verdict != "questions":
        if raw_questions:
            return None
        return TriageResult(verdict, reason, summary, [])
    if not summary or not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= MAX_QUESTIONS:
        return None
    questions = [_question(q) for q in raw_questions]
    if any(q is None for q in questions) or len({q.id for q in questions if q}) != len(questions):
        return None
    return TriageResult(verdict, reason, summary, questions)  # type: ignore[arg-type]


def _apply(night: Night, item: Item, round: int, result: TriageResult) -> None:
    plane, ref = night.plane, item.ref
    desc = desc_hash(item.description)
    said = _said(result.reason, SPEAKER)
    limit = night.cfg.limits.max_question_rounds
    waiting = "bau-questions" in item.labels
    if result.verdict == "ready":
        # Label off first: a ready verdict left beside bau-questions would hold the item for ever,
        # whereas a missing verdict just means triage runs again (the description hash is stale).
        if waiting:
            plane.remove_labels(ref, ["bau-questions"])
        plane.comment(ref, triage_comment("ready", round, desc, said))
    elif result.verdict == "questions" and round <= limit:
        plane.add_labels(ref, ["bau-questions"])
        try:
            plane.comment(ref, questions_comment(round, result.summary, result.questions))
        except Exception:
            # Without the questions the label would hold the item for ever: take it back so it is
            # triaged again another night.
            plane.remove_labels(ref, ["bau-questions"])
            raise
        plane.comment(ref, triage_comment("questions", round, desc, said))
        night.report.asked += 1
    else:
        if result.verdict == "questions":
            said += f" (all {limit} rounds of questions were used, so this counts as too big)"
        # not-bau and the verdict first: a failure part-way leaves the item held, never unlabelled.
        plane.add_labels(ref, ["not-bau"])
        plane.comment(ref, triage_comment("too_big", round, desc, said))
        plane.remove_labels(ref, ["BAU", "bau-questions"] if waiting else ["BAU"])
    night.report.triaged += 1


def _candidates(night: Night, ident: str) -> list[tuple[Item, str, list[Comment]]]:
    from bau.runloop import FINISHED_STATES, repo_for

    worker = night.cfg.identities.worker_plane_email
    open_refs = {r for prs in night._open.values() for p in prs if (r := night.ref_of(p))}
    out = []
    for it in night.plane.list_items(ident, "BAU"):
        # Cheap checks before fetching comments; finished items and live PRs are past triage.
        if any(label in NEVER_TRIAGE for label in it.labels):
            continue
        if it.state.casefold() in FINISHED_STATES or it.ref in open_refs:
            continue
        repo = repo_for(it, night.projects)
        if repo is None or repo not in night.installed:
            continue
        comments = night.plane.comments(it.ref)
        if not needs_triage(it, comments, worker):
            continue
        owner = night.cfg.identities.owner_plane_email
        if triage_failures(comments, owner, worker) >= night.cfg.limits.max_triage_failures:
            night.report.declined.append({"ref": it.ref, "reason": TRIAGE_GAVE_UP, "at": night.now_iso()})
            continue
        out.append((it, repo, comments))
    return out


def _failed(night: Night, ref: str, why: str) -> None:
    """Note a session that reached no verdict, so repeated failures stop. Best effort."""
    night.log(f"triage {ref}: {why}")
    try:
        night.plane.comment(ref, triage_failed_comment(_clean_reason(why)))
    except Exception as e:
        night.log(f"triage {ref}: could not record the failure: {type(e).__name__}: {e}")


def triage_pass(night: Night) -> None:
    from bau.runloop import model_for

    ids, limits = night.cfg.identities, night.cfg.limits
    for ident in sorted(night.projects):
        for it, repo, comments in _candidates(night, ident):
            if night.dry_run:
                night.report.planned.append(f"triage {it.ref} -> {repo}")
                continue
            if not night.may_start():
                return
            round = current_round(comments, ids.worker_plane_email)
            brief = build_triage_brief(
                it,
                repo,
                comments,
                round=round,
                limits=limits,
                owner_email=ids.owner_plane_email,
                worker_email=ids.worker_plane_email,
            )
            try:
                with night.repos.scratch_clone(repo) as checkout:
                    result = night.agent_session(
                        name=f"bau-triage-{it.ref.lower()}",
                        checkout=checkout,
                        bare=None,
                        brief=brief,
                        model=model_for(it, night.cfg.models),
                        log_name=f"triage-{it.ref}",
                        github_token="",
                        budget_usd=limits.triage_budget_usd,
                        wall_clock_minutes=limits.triage_wall_clock_minutes,
                    )
            except Exception as e:  # a failed clone must not end the night
                night.log(f"triage {it.ref}: could not run a session: {type(e).__name__}: {e}")
                continue
            if result.outcome == "rate_limited":
                return
            if result.outcome != "done":
                _failed(night, it.ref, f"session ended with {result.outcome}: {result.reason}")
                continue
            parsed = parse_triage(result.result_text)
            if parsed is None:
                _failed(night, it.ref, "no valid verdict in the output")
                continue
            try:
                _apply(night, it, round, parsed)
            except Exception as e:
                night.log(f"triage {it.ref}: could not record the verdict: {type(e).__name__}: {e}")
