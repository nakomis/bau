from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bau.checkout import Repos
from bau.config import ProjectConfig
from bau.github import GitHubClient
from bau.lock import run_lock
from bau.plane import Comment, PlaneClient, split_ref
from bau.runloop import Night, previous_state

RUN_IN_PROGRESS = "a BAU run is in progress; try again after it finishes"


class AlreadyAbandoned(RuntimeError):
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
    note: str = "",
    lock_path: Path | None = None,
) -> AbandonResult:
    if lock_path is None:
        return _abandon(ref, plane, github_factory, repos, projects, worker_login, note)
    with run_lock(lock_path) as held:
        if not held:
            raise RunInProgress(RUN_IN_PROGRESS)
        return _abandon(ref, plane, github_factory, repos, projects, worker_login, note)


def _abandon(ref, plane, github_factory, repos, projects, worker_login, note) -> AbandonResult:
    comments = plane.comments(ref)  # chronological; raises for an unknown ref
    picked_at = _last_index(comments, "[bau:picked-up")
    if picked_at < 0:
        raise AlreadyAbandoned(f"{ref} has no active BAU pick-up to abandon")
    if _last_index(comments, "[bau:abandoned]") > picked_at:
        raise AlreadyAbandoned(f"{ref} has already been abandoned")
    restored = previous_state(comments) or "New"
    suffix = f" Note: {note}" if note else ""

    ident, _ = split_ref(ref)
    project = projects.get(ident)
    pr_number = None
    branch = None
    if project:
        gh = github_factory(project.repo)
        for pr in gh.open_prs(project.repo, worker_login):
            if Night.ref_of(pr) == ref and pr.head_ref.startswith(ref.lower() + "-"):
                gh.comment(
                    project.repo, pr.number, "[bau] Abandoned by Martin from the BAU dashboard." + suffix
                )
                gh.close_pr(project.repo, pr.number)
                pr_number, branch = pr.number, pr.head_ref
                break
    if branch is None:
        parts = comments[picked_at].text.split("`")
        if len(parts) >= 3:
            branch = parts[1]
    if branch:
        repos.delete_checkout(branch)
    plane.set_state(ref, restored)
    plane.remove_labels(ref, ["BAU"])
    plane.comment(ref, f"[bau:abandoned] by Martin. Returned to {restored}; BAU label removed." + suffix)
    return AbandonResult(ref, pr_number, restored)
