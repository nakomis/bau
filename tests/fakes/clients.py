from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from bau.checkout import Repos
from bau.github import PR, Thread
from bau.plane import Comment, Item
from bau.session import SessionResult


class FakePlane:
    def __init__(self, items: dict[str, list[Item]], comments: dict[str, list[Comment]] | None = None):
        self.items, self.comments_by_ref, self.calls = items, comments or {}, []

    def list_items(self, identifier, label):
        return [i for i in self.items.get(identifier, []) if label in i.labels]

    def get_item(self, ref):
        found = next((i for items in self.items.values() for i in items if i.ref == ref), None)
        if found is None:
            raise ValueError(f"unknown work item {ref}")
        return found

    def comments(self, ref):
        return self.comments_by_ref.get(ref, [])

    def project_url(self, ref):
        return f"https://plane.test/{ref}"

    def comment(self, ref, markdown):
        self.calls.append(("comment", (ref, markdown)))
        return "cid"

    def set_state(self, ref, state):
        self.calls.append(("set_state", (ref, state)))
        for items in self.items.values():
            items[:] = [replace(i, state=state) if i.ref == ref else i for i in items]

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
    ):
        self.open, self.threads_by_pr, self.ci, self.protected = (
            open_prs or {},
            threads or {},
            ci or {},
            protected or {},
        )
        self.merged, self.calls = merged or {}, []
        self.comments_by_pr = issue_comments or {}
        self.summaries = summaries

    def open_prs(self, repo, author):
        return self.open.get(repo, [])

    def pr(self, repo, number):
        return next(p for p in self.open.get(repo, []) if p.number == number)

    def merged_prs_since(self, repo, author, since_iso):
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
    def __init__(self, results: list[SessionResult], on_run: Callable[[], None] | None = None):
        self.results, self.briefs, self.on_run = list(results), [], on_run

    def run(self, *, name, checkout, bare, brief, model, github_token, oauth_token, log_path):
        self.briefs.append((name, model, brief))
        if self.on_run:
            self.on_run()
        return self.results.pop(0)


class FakeRepos(Repos):
    def __init__(self, root: Path, rebase_ok: bool = True, stats=(1, 10)):
        self.root, self.rebase_ok, self.stats, self.calls = root, rebase_ok, stats, []
        self.repos_root, self.work_root = root / "repos", root / "work"

    def ensure_bare(self, repo, token, origin_url=None):
        self.calls.append(("ensure_bare", repo))
        return self.bare_path(repo)

    def fetch(self, repo, token):
        self.calls.append(("fetch", repo))

    def create_checkout(self, repo, branch):
        self.calls.append(("create_checkout", repo, branch))
        return self.checkout_path(branch)

    def delete_checkout(self, branch):
        self.calls.append(("delete_checkout", branch))

    def rebase_onto_main(self, branch, token):
        self.calls.append(("rebase", branch))
        return self.rebase_ok

    def diff_stats(self, branch):
        return self.stats
