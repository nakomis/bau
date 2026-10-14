from __future__ import annotations

from dataclasses import dataclass, field

from bau.github import Thread, ThreadComment
from bau.plane import Comment

WORKER_MARK = "[bau]"


def ack_marker(comment_id: str) -> str:
    return f"[bau:ack {comment_id}]"


@dataclass(frozen=True)
class Feedback:
    threads: list[Thread] = field(default_factory=list)
    plane_comments: list[Comment] = field(default_factory=list)
    conflict: bool = False
    ci_red: bool = False

    @property
    def empty(self) -> bool:
        return not (self.threads or self.plane_comments or self.conflict or self.ci_red)


def _login(login: str) -> str:
    return login.removesuffix("[bot]")


def is_worker(comment: ThreadComment, worker_login: str) -> bool:
    """GraphQL drops the "[bot]" suffix that REST and config carry, so compare without it on both
    sides; a reply marked [bau] is the worker's whatever login it arrives under."""
    return _login(comment.author) == _login(worker_login) or comment.body.startswith(WORKER_MARK)


def pending_threads(threads: list[Thread], owner_login: str, worker_login: str) -> list[Thread]:
    out = []
    for th in threads:
        if th.resolved or not th.comments:
            continue
        if not any(c.author == owner_login for c in th.comments):
            continue
        if is_worker(th.comments[-1], worker_login):
            continue
        out.append(th)
    return out


def pending_plane_comments(comments: list[Comment], owner_email: str, worker_email: str) -> list[Comment]:
    acked_ids: set[str] = set()
    for c in comments:
        if c.author_email == worker_email:
            for other in comments:
                if ack_marker(other.id) in c.text:
                    acked_ids.add(other.id)
    return [c for c in comments if c.author_email == owner_email and c.id not in acked_ids]
