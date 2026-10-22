from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from bau.checkout import Repos
from bau.github import PR, IssueComment, Review, Thread
from bau.plane import Comment, Item, html_to_text, markdown_to_html
from bau.session import SessionResult


class FakePlane:
    def __init__(self, items: dict[str, list[Item]], comments: dict[str, list[Comment]] | None = None):
        self.items, self.comments_by_ref, self.calls = items, comments or {}, []
        self.unreachable: Exception | None = None
        self.forbidden: set[str] = set()  # projects BAU's Plane account cannot read

    def list_items(self, identifier, label):
        return [i for i in self.items.get(identifier, []) if label in i.labels]

    def list_open_items(self, identifier):
        return [i for i in self.items.get(identifier, []) if i.state not in ("Done", "Cancelled")]

    def create_item(self, identifier, name, description_md, state=None):
        self.calls.append(("create_item", (identifier, name, description_md, state)))
        ref = f"{identifier}-99"
        self.items.setdefault(identifier, []).append(
            Item(ref, ref.lower(), "p", name, description_md, "New", [], None, "")
        )
        return ref

    def get_item(self, ref):
        found = next((i for items in self.items.values() for i in items if i.ref == ref), None)
        if found is None:
            raise ValueError(f"unknown work item {ref}")
        return found

    def comments(self, ref):
        return self.comments_by_ref.get(ref, [])

    def project_url(self, ref):
        return f"https://plane.test/{ref}"

    def _project(self, identifier):
        if self.unreachable:
            raise self.unreachable
        if identifier in self.forbidden:
            raise RuntimeError(f"Plane GET /projects/{identifier}/states/ -> 403")
        return {"project": {"identifier": identifier}}

    def comment(self, ref, markdown):
        self.calls.append(("comment", (ref, markdown)))
        self.comments_by_ref.setdefault(ref, []).append(
            Comment(f"c{len(self.calls)}", "bau@nakomis.com", "", html_to_text(markdown_to_html(markdown)))
        )
        return "cid"

    def set_state(self, ref, state):
        self.calls.append(("set_state", (ref, state)))
        for items in self.items.values():
            items[:] = [replace(i, state=state) if i.ref == ref else i for i in items]

    def _reassign(self, ref, assignees):
        for items in self.items.values():
            items[:] = [replace(i, assignees=assignees) if i.ref == ref else i for i in items]

    def assign(self, ref, email):
        self.calls.append(("assign", (ref, email)))
        self._reassign(ref, [email])

    def unassign(self, ref):
        self.calls.append(("unassign", (ref,)))
        self._reassign(ref, [])

    def _relabel(self, ref, names, add: bool):
        delta = set(names)
        for items in self.items.values():
            items[:] = [
                replace(i, labels=sorted((set(i.labels) | delta) if add else (set(i.labels) - delta)))
                if i.ref == ref
                else i
                for i in items
            ]

    def add_labels(self, ref, names):
        self.calls.append(("add_labels", (ref, names)))
        self._relabel(ref, names, True)

    def remove_labels(self, ref, names):
        self.calls.append(("remove_labels", (ref, names)))
        self._relabel(ref, names, False)

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))
            return "cid"

        return record


class FakeGitHub:
    def __init__(
        self,
        open_prs: dict[str, list[PR]] | None = None,
        threads: dict[int, list[Thread]] | None = None,
        ci: dict[str, str | list[str]] | None = None,
        protected: dict[str, bool] | None = None,
        merged: dict[str, list[PR]] | None = None,
        issue_comments: dict[int, list[str]] | None = None,
        summaries: str = "",
        comment_rows: dict[int, list[IssueComment]] | None = None,
        reviews: dict[int, list[Review]] | None = None,
    ):
        self.rows_by_pr, self.reviews_by_pr = comment_rows or {}, reviews or {}
        self.open, self.threads_by_pr, self.ci, self.protected = (
            open_prs or {},
            threads or {},
            ci or {},
            protected or {},
        )
        self.merged, self.calls = merged or {}, []
        self.comments_by_pr = issue_comments or {}
        self.summaries = summaries
        self.merged_since: list[str] = []

    def open_prs(self, repo, author):
        return self.open.get(repo, [])

    def pr(self, repo, number):
        return next(p for p in self.open.get(repo, []) if p.number == number)

    def merged_prs_since(self, repo, author, since_iso):
        self.merged_since.append(since_iso)
        return self.merged.get(repo, [])

    def review_threads(self, repo, number):
        return self.threads_by_pr.get(number, [])

    def ci_status(self, repo, sha):
        value = self.ci.get(sha, "none")
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def check_summaries(self, repo, sha):
        return self.summaries

    def issue_comments(self, repo, number):
        return list(self.comments_by_pr.get(number, []))

    def issue_comment_rows(self, repo, number):
        """Seeded rows, then plain-string comments (the worker's own, posted via comment())."""
        rows = list(self.rows_by_pr.get(number, []))
        return rows + [
            IssueComment(1000 + i, "nakomis-bau[bot]", b)
            for i, b in enumerate(self.comments_by_pr.get(number, []))
        ]

    def reviews(self, repo, number):
        return list(self.reviews_by_pr.get(number, []))

    def comment(self, repo, number, body):
        self.calls.append(("comment", (repo, number, body)))
        self.comments_by_pr.setdefault(number, []).append(body)

    def branch_protected(self, repo, branch="main"):
        return self.protected.get(repo, True)

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))

        return record


class FakeRunner:
    def __init__(self, results: list[SessionResult | Exception], on_run: Callable[[], None] | None = None):
        self.results, self.briefs, self.on_run = list(results), [], on_run

    def run(
        self,
        *,
        name,
        checkout,
        bare,
        brief,
        model,
        github_token,
        oauth_token,
        log_path,
        budget_usd=None,
        wall_clock_minutes=None,
    ):
        self.briefs.append((name, model, brief))
        self.overrides = {"budget_usd": budget_usd, "wall_clock_minutes": wall_clock_minutes}
        if self.on_run:
            self.on_run()
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeRepos(Repos):
    def __init__(self, root: Path, existing: tuple[str, ...] = ()):
        self.root, self.calls = root, []
        self.checkouts = set(existing)
        self.repos_root, self.work_root = root / "repos", root / "work"

    def ensure_bare(self, repo, token, origin_url=None):
        self.calls.append(("ensure_bare", repo))
        return self.bare_path(repo)

    def fetch(self, repo, token):
        self.calls.append(("fetch", repo))

    def create_checkout(self, repo, branch):
        self.calls.append(("create_checkout", repo, branch))
        if branch in self.checkouts:
            raise RuntimeError(f"git clone failed: destination path for {branch} already exists")
        self.checkouts.add(branch)
        return self.checkout_path(branch)

    def has_checkout(self, branch):
        return branch in self.checkouts

    @contextlib.contextmanager
    def scratch_clone(self, repo):
        self.calls.append(("scratch_clone", repo))
        self.work_root.mkdir(parents=True, exist_ok=True)
        dest = Path(tempfile.mkdtemp(prefix="triage-", dir=self.work_root))
        try:
            yield dest
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def checkout_branch(self, repo, branch, token=""):
        self.calls.append(("checkout_branch", repo, branch))
        if branch in self.checkouts:
            raise RuntimeError(f"git clone failed: destination path for {branch} already exists")
        self.checkouts.add(branch)
        return self.checkout_path(branch)

    def delete_checkout(self, branch):
        self.calls.append(("delete_checkout", branch))
        self.checkouts.discard(branch)
