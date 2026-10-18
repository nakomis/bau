from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bau.checkout import Repos
from bau.config import ProjectConfig
from bau.github import GitHubClient
from bau.lock import run_lock
from bau.plane import Comment, PlaneClient, split_ref
from bau.runloop import Night, previous_state, repo_for

log = logging.getLogger(__name__)
REF_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
FINISHED_STATES = ("done", "cancelled", "archived", "ready for test")
RUN_IN_PROGRESS = "another BAU operation is in progress; try again shortly"


class AlreadyAbandoned(RuntimeError):
    pass


class InvalidRef(ValueError):
    pass


class UnknownProject(ValueError):
    pass


class RunInProgress(RuntimeError):
    pass


@dataclass(frozen=True)
class AbandonResult:
    ref: str
    pr: int | None
    restored_state: str


def _last_index(comments: list[Comment], marker: str) -> int:
    return max((i for i, c in enumerate(comments) if marker in c.text), default=-1)


def abandon(
    ref: str,
    *,
    plane: PlaneClient,
    github_factory: Callable[[str], GitHubClient],
    repos: Repos,
    projects: dict[str, ProjectConfig],
    worker_login: str,
    worker_email: str,
    note: str = "",
    lock_path: Path | None = None,
) -> AbandonResult:
    ref = ref.strip()
    if not REF_PATTERN.match(ref):
        raise InvalidRef("invalid ref")
    if split_ref(ref)[0] not in projects:
        raise UnknownProject("unknown project")
    args = (ref, plane, github_factory, repos, projects, worker_login, worker_email, clean_note(note))
    if lock_path is None:
        return _abandon(*args)
    with run_lock(lock_path) as held:
        if not held:
            raise RunInProgress(RUN_IN_PROGRESS)
        return _abandon(*args)


def clean_note(note: str) -> str:
    """One line, no bracket characters: a note must never be able to forge a [bau:...] marker."""
    return " ".join(note.split()).replace("[", "(").replace("]", ")")[:500]


def _own_branch(ref: str, branch: str | None) -> str | None:
    if branch and branch.startswith(ref.lower() + "-"):
        return branch
    if branch:
        log.warning("not deleting a checkout for %s: branch does not belong to it", ref)
    return None


def _abandon(ref, plane, github_factory, repos, projects, worker_login, worker_email, note) -> AbandonResult:
    item = plane.get_item(ref)
    if item.state.lower() in FINISHED_STATES or "BAU" not in item.labels:
        raise AlreadyAbandoned(f"nothing to abandon: {ref} is {item.state}")
    # Only the worker's own comments carry markers; anyone else's are just text.
    comments = [c for c in plane.comments(ref) if c.author_email == worker_email]
    picked_at = _last_index(comments, "[bau:picked-up")
    if picked_at < 0:
        raise AlreadyAbandoned(f"{ref} has no active BAU pick-up to abandon")
    if _last_index(comments, "[bau:abandoned]") > picked_at:
        raise AlreadyAbandoned(f"{ref} has already been abandoned")
    restored = previous_state(comments, worker_email) or "New"
    suffix = f" Note: {note}" if note else ""

    repo = repo_for(item, projects)
    if repo is None:
        raise UnknownProject(f"no repository for {ref}")
    gh = github_factory(repo)
    # Restore the state first: if anything later fails, the item is no longer eligible for pick-up
    # and the PR and label are still there for a retry.
    plane.set_state(ref, restored)
    pr_number = None
    branch = None
    for pr in gh.open_prs(repo, worker_login):
        if Night.ref_of(pr) == ref and pr.head_ref.startswith(ref.lower() + "-"):
            # Close first: a retry after a failed close must not post the comment twice.
            gh.close_pr(repo, pr.number)
            gh.comment(repo, pr.number, "[bau] Abandoned by Martin from the BAU dashboard." + suffix)
            pr_number, branch = pr.number, pr.head_ref
            break
    if branch is None:
        parts = comments[picked_at].text.split("`")
        if len(parts) >= 3:
            branch = parts[1]
    branch = _own_branch(ref, branch)
    if branch:
        try:
            repos.delete_checkout(branch)
        except ValueError:
            log.warning("refused to delete a checkout for %s", ref)
    # not-bau before BAU comes off: a failure between the two must not leave the item unlabelled.
    plane.add_labels(ref, ["not-bau"])
    plane.remove_labels(ref, ["BAU"])
    plane.comment(ref, f"[bau:abandoned] by Martin. Returned to {restored}; marked not-bau." + suffix)
    return AbandonResult(ref, pr_number, restored)
