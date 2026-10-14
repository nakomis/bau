from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bau.checkout import Repos
from bau.config import Config, Models, ProjectConfig
from bau.feedback import Feedback, ack_marker, pending_plane_comments, pending_threads
from bau.github import PR, GitHubApp, GitHubClient, Token
from bau.outcome import diff_too_big
from bau.plane import Comment, Item, PlaneClient, split_ref
from bau.policy import Policy
from bau.prompts import build_brief
from bau.session import SessionResult, SessionRunner
from bau.telemetry import RateLimit, parse_stream_line, rate_limit_from_event

PICKED = re.compile(r"\[bau:picked-up from=([^\]]+)\]")
REPO_LINE = re.compile(r"^repo:\s*([\w.-]+/[\w.-]+)\s*$", re.MULTILINE)
REF_IN_BODY = re.compile(r"Plane:\s*([A-Z0-9]+-\d+)")
REF_IN_TITLE = re.compile(r"\(([A-Z0-9]+-\d+)\)")
CI_FIX_MARK = "[bau:ci-fix]"
FINISHED_STATES = ("ready for test", "done", "archived", "cancelled")
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
MERGED_WINDOW = timedelta(days=30)
NO_CI_GRACE_S = 180
NO_CI_COMMENT = "[bau] No CI checks ran on this repository; marked ready without them."
# Plane is the ledger of failed attempts: these are the worker's own comments that end an attempt.
FAILED_ATTEMPT_PREFIXES = (
    "[bau] Too big for BAU",
    "[bau] Session ended with",
    "[bau] Session finished but no PR",
)
MAX_FAILED_ATTEMPTS = 2
TRIED_TWICE = "BAU tried this twice; comment on the ticket to ask for another attempt"


def branch_name(ref: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
    return f"{ref.lower()}-{slug}"


def repo_for(item: Item, projects: dict[str, ProjectConfig]) -> str | None:
    m = REPO_LINE.search(item.description or "")
    if m:
        return m.group(1)
    ident, _ = split_ref(item.ref)
    p = projects.get(ident)
    return p.repo if p else None


def model_for(item: Item, models: Models) -> str:
    return models.fable if models.fable_label in item.labels else models.default


def previous_state(comments: list[Comment], worker_email: str) -> str | None:
    for c in reversed(comments):
        if c.author_email != worker_email:
            continue
        m = PICKED.search(c.text)
        if m:
            return m.group(1).strip()
    return None


@dataclass
class NightReport:
    stop_reason: str = "ok"
    sessions: int = 0
    prs_opened: int = 0
    feedback_handled: int = 0
    declined: list[dict] = field(default_factory=list)
    latest: RateLimit | None = None
    telemetry: str = "ok"
    planned: list[str] = field(default_factory=list)


class Night:
    def __init__(
        self,
        cfg: Config,
        projects: dict[str, ProjectConfig],
        plane: PlaneClient,
        gh_app: GitHubApp,
        github_factory: Callable[[str], GitHubClient],
        repos: Repos,
        runner: SessionRunner,
        clock: Callable[[], datetime],
        *,
        oauth_token: str,
        dry_run: bool = False,
        log: Callable[..., None] = print,
        sleep: Callable[[float], None] = time.sleep,
        paused: Callable[[], bool] | None = None,
    ):
        self.cfg, self.projects, self.plane, self.gh_app = cfg, projects, plane, gh_app
        self.github_factory, self.repos, self.runner, self.clock = github_factory, repos, runner, clock
        self.oauth_token, self.dry_run, self.log, self.sleep = oauth_token, dry_run, log, sleep
        self.paused = paused or (lambda: cfg.paused)
        self.policy = Policy(cfg.limits, cfg.schedule, clock)
        self.report = NightReport()
        self._tokens: dict[str, Token] = {}
        self._open: dict[str, list[PR]] = {}
        self._open_total = 0
        self._merged: dict[str, PR] = {}

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def ref_of(pr: PR) -> str | None:
        m = REF_IN_BODY.search(pr.body or "") or REF_IN_TITLE.search(pr.title or "")
        return m.group(1) if m else None

    def pr_ref(self, pr: PR) -> str | None:
        """The ref the runner may act on: the body or title names it AND the branch carries it."""
        ref = self.ref_of(pr)
        if not ref:
            return None
        if not pr.head_ref.startswith(ref.lower() + "-"):
            self.log(f"skip PR #{pr.number}: branch {pr.head_ref!r} does not match {ref}")
            return None
        return ref

    def token(self, repo: str) -> str:
        cached = self._tokens.get(repo)
        if cached is not None:
            expires = datetime.fromisoformat(cached.expires_at.replace("Z", "+00:00"))
            if expires - TOKEN_REFRESH_MARGIN > self.clock():
                return cached.value
        self._tokens[repo] = self.gh_app.installation_token(repo)
        return self._tokens[repo].value

    def now_iso(self) -> str:
        return self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z")

    def probe(self) -> RateLimit | None:
        """One-line session to read the current usage windows. Never --bare: that ignores the login."""
        cmd = [
            "claude",
            "-p",
            "Reply with exactly: ok",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "claude-haiku-5-5",
            "--max-budget-usd",
            "0.05",
            "--disable-slash-commands",
        ]
        home = Path.home()
        env = {
            **os.environ,
            "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token,
            "HOME": str(home),
            "PATH": os.pathsep.join([str(home / ".local/bin"), os.environ.get("PATH", "")]),
        }
        try:
            proc = subprocess.run(cmd, input="", capture_output=True, text=True, env=env, timeout=120)
        except FileNotFoundError:
            self.log(
                f"PROBE FAILED: the claude CLI was not found on PATH={env['PATH']}; "
                "usage is unknown and the night runs fail-closed (telemetry: missing)"
            )
            return None
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"PROBE FAILED: {type(e).__name__}: {e}; telemetry: missing")
            return None
        latest = None
        for line in proc.stdout.splitlines():
            obj = parse_stream_line(line)
            if obj and (rl := rate_limit_from_event(obj)):
                latest = rl
        return latest

    def wait_for_ci(
        self,
        gh: GitHubClient,
        repo: str,
        sha: str,
        timeout_s: float = 1800,
        poll_s: float = 30,
        none_grace_s: float = NO_CI_GRACE_S,
    ) -> str:
        """Poll until CI settles. "none" (no checks yet) is pending only for a short grace: a repo
        without CI must not hold the night for the full timeout."""
        status = gh.ci_status(repo, sha)
        waited = 0.0
        while waited < timeout_s and (status == "pending" or (status == "none" and waited < none_grace_s)):
            self.sleep(poll_s)
            waited += poll_s
            status = gh.ci_status(repo, sha)
        return status

    def errored(self, ref: str, e: Exception) -> None:
        """One failing item must not end the night: record it, tell the ticket if we can, move on."""
        reason = f"error: {type(e).__name__}: {e}"[:300]
        self.report.declined.append({"ref": ref, "reason": reason, "at": self.now_iso()})
        self.log(f"{ref}: {reason}")
        if self.dry_run:
            return
        text = f"[bau] Error while working this ticket: {reason}"
        try:
            worker = self.cfg.identities.worker_plane_email
            if not any(c.author_email == worker and text in c.text for c in self.plane.comments(ref)):
                self.plane.comment(ref, text)
        except Exception as inner:  # noqa: BLE001 - best effort only
            self.log(f"{ref}: could not post the error comment: {inner}")

    def decline(self, item: Item, reason: str, seen: list[Comment] | None = None) -> None:
        """Record and comment once. `seen` limits the de-duplication to those comments."""
        self.report.declined.append({"ref": item.ref, "reason": reason, "at": self.now_iso()})
        self.log(f"decline {item.ref}: {reason}")
        if self.dry_run:
            return
        text = f"[bau] Not picked up: {reason}"
        pool = self.plane.comments(item.ref) if seen is None else seen
        if not any(text in c.text for c in pool):
            self.plane.comment(item.ref, text)

    def may_start(self, *, new_ticket: bool) -> bool:
        d = self.policy.can_start_session(
            latest=self.report.latest,
            open_prs=self._open_total,
            sessions_started=self.report.sessions,
            paused=self.paused(),
            new_ticket=new_ticket,
        )
        if d.reason == "telemetry-missing":
            self.report.telemetry = "missing"
        if not d.allowed:
            self.report.stop_reason = d.reason
        return d.allowed

    def session(self, *, item: Item, repo: str, branch: str, brief: str, model: str) -> SessionResult:
        self.report.sessions += 1
        result = self.runner.run(
            name=f"bau-{item.ref.lower()}",
            checkout=self.repos.checkout_path(branch),
            bare=self.repos.bare_path(repo),
            brief=brief,
            model=model,
            github_token=self.token(repo),
            oauth_token=self.oauth_token,
            log_path=self.cfg.paths.logs / f"{self.clock():%Y-%m-%d}-{item.ref}.log",
        )
        # The newest reading wins, even when it is "none": a stale figure must not keep the night going.
        self.report.latest = result.latest
        if result.latest is None:
            self.report.telemetry = "missing"
        if result.outcome == "rate_limited":
            self.report.stop_reason = "rejected"
        return result

    def brief(
        self,
        item: Item,
        repo: str,
        branch: str,
        feedback: Feedback | None,
        ci_log: str | None,
        previous_attempt: str | None = None,
    ) -> str:
        ids = self.cfg.identities
        return build_brief(
            item,
            repo,
            branch,
            feedback,
            ci_log,
            self.cfg.limits,
            owner_login=ids.github_login,
            owner_email=ids.owner_plane_email,
            previous_attempt=previous_attempt,
        )

    # -- passes --------------------------------------------------------------
    def preflight(self) -> bool:
        """One Plane call and one GitHub call before anything else: an unreachable service ends the
        night with a snapshot that says so, rather than a crash part-way through."""
        try:
            ident, project = next(iter(self.projects.items()))
            self.plane._project(ident)
            self.token(project.repo)
        except Exception as e:
            self.report.stop_reason = f"preflight: {type(e).__name__}"
            self.log(f"preflight failed: {type(e).__name__}: {e}")
            return False
        return True

    def run(self) -> NightReport:
        if not self.preflight():
            return self.report
        self.report.latest = self.probe()
        if self.report.latest is None:
            self.report.telemetry = "missing"
        self.log(f"preflight: latest={self.report.latest}")
        for repo in sorted({p.repo for p in self.projects.values()}):
            gh = self.github_factory(repo)
            self._open[repo] = gh.open_prs(repo, self.cfg.identities.worker_github_login)
            if not self.dry_run:
                self.repos.ensure_bare(repo, self.token(repo))
                self.repos.fetch(repo, self.token(repo))
        self._open_total = sum(len(v) for v in self._open.values())
        # Neither starts a session, so neither waits for the policy check.
        self.merge_pass()
        self.ready_pass()
        if not self.may_start(new_ticket=False):
            return self.report
        self.feedback_pass()
        self.new_tickets_pass()
        return self.report

    def feedback_pass(self) -> None:
        for repo, prs in self._open.items():
            gh = self.github_factory(repo)
            for pr in sorted(prs, key=lambda p: p.created_at):
                if self.report.stop_reason != "ok":
                    return
                ref = self.pr_ref(pr)
                if not ref:
                    continue
                try:
                    self.feedback_one(gh, repo, pr, ref)
                except Exception as e:
                    self.errored(ref, e)

    def mark_ready_checked(self, gh: GitHubClient, repo: str, pr: PR, ref: str, ci: str) -> None:
        """Mark a settled PR ready (CI green, or no CI after the grace) unless it is over the size limit."""
        files, lines = pr.changed_files, pr.additions + pr.deletions
        if diff_too_big(files, lines, self.cfg.limits):
            item = self.plane.get_item(ref)
            back = (
                previous_state(self.plane.comments(ref), self.cfg.identities.worker_plane_email) or item.state
            )
            self.close_too_big(item, repo, gh, pr, pr.head_ref, back, f"{files} files / {lines} lines")
            return
        self.mark_ready(gh, repo, pr, ci)
        if ci == "none":
            self.plane.comment(ref, f"[bau] PR #{pr.number} has no CI checks; marked ready for review.")
        else:
            self.plane.comment(ref, f"[bau] PR #{pr.number} CI is now green; marked ready for review.")

    @staticmethod
    def mark_ready(gh: GitHubClient, repo: str, pr: PR, ci: str) -> None:
        gh.mark_ready(pr.node_id)
        if ci == "none":
            gh.comment(repo, pr.number, NO_CI_COMMENT)

    def pending_feedback(self, gh: GitHubClient, repo: str, pr: PR, ref: str) -> tuple[list, list]:
        ids = self.cfg.identities
        threads = pending_threads(
            gh.review_threads(repo, pr.number), ids.github_login, ids.worker_github_login
        )
        plane_comments = pending_plane_comments(
            self.plane.comments(ref), ids.owner_plane_email, ids.worker_plane_email
        )
        return threads, plane_comments

    def ready_pass(self) -> None:
        """Session-free: mark ready every draft whose CI has settled clean and that has no feedback."""
        for repo, prs in self._open.items():
            gh = self.github_factory(repo)
            for pr in sorted(prs, key=lambda p: p.created_at):
                if not pr.draft:
                    continue
                ref = self.pr_ref(pr)
                if not ref:
                    continue
                try:
                    self.ready_one(gh, repo, pr, ref)
                except Exception as e:
                    self.errored(ref, e)

    def ready_one(self, gh: GitHubClient, repo: str, pr: PR, ref: str) -> None:
        # Drafts here were opened on an earlier night, so "none" is already past its grace.
        status = gh.ci_status(repo, pr.head_sha)
        if status not in ("green", "none"):
            return
        try:
            self.plane.get_item(ref)
        except ValueError as e:
            self.log(f"skip PR #{pr.number}: {e}")
            return
        threads, plane_comments = self.pending_feedback(gh, repo, pr, ref)
        if threads or plane_comments or pr.mergeable_state == "dirty":
            return
        if self.dry_run:
            self.report.planned.append(f"ready {ref} -> {repo} PR #{pr.number}")
            return
        self.mark_ready_checked(gh, repo, pr, ref, status)

    def feedback_one(self, gh: GitHubClient, repo: str, pr: PR, ref: str) -> None:
        try:
            item = self.plane.get_item(ref)
        except ValueError as e:
            self.log(f"skip PR #{pr.number}: {e}")
            return
        limit = self.cfg.limits.ci_fix_sessions_per_pr
        threads, plane_comments = self.pending_feedback(gh, repo, pr, ref)
        status = gh.ci_status(repo, pr.head_sha)
        ci_red, fixes_so_far = False, 0
        if status == "red":
            bodies = gh.issue_comments(repo, pr.number)
            fixes_so_far = sum(CI_FIX_MARK in b for b in bodies)
            ci_red = fixes_so_far < limit
            if not ci_red and not self.dry_run:
                giving_up = f"[bau] CI still red after {limit} fix sessions; leaving this for Martin."
                if not any(giving_up in b for b in bodies):
                    gh.comment(repo, pr.number, giving_up)
        fb = Feedback(threads, plane_comments, conflict=pr.mergeable_state == "dirty", ci_red=ci_red)
        if fb.empty:  # a clean green draft was handled by ready_pass
            return
        if self.dry_run:
            self.report.planned.append(f"feedback {ref} -> {repo} PR #{pr.number}")
            return
        if not self.may_start(new_ticket=False):
            return
        # Never reuse a checkout an earlier session wrote: delete it and clone the branch afresh.
        self.repos.delete_checkout(pr.head_ref)
        self.repos.checkout_branch(repo, pr.head_ref, self.token(repo))
        ci_log = gh.check_summaries(repo, pr.head_sha) if ci_red else None
        result = self.session(
            item=item,
            repo=repo,
            branch=pr.head_ref,
            brief=self.brief(item, repo, pr.head_ref, fb, ci_log),
            model=model_for(item, self.cfg.models),
        )
        if ci_red:
            gh.comment(repo, pr.number, f"{CI_FIX_MARK} {fixes_so_far + 1}/{limit}")
        if result.outcome != "done":
            gh.comment(
                repo, pr.number, f"[bau] Feedback session ended with {result.outcome}: {result.reason}"
            )
            return
        self.report.feedback_handled += 1
        for c in plane_comments:
            self.plane.comment(ref, f"[bau] Addressed in PR #{pr.number}. {ack_marker(c.id)}")
        if pr.draft:
            latest = gh.pr(repo, pr.number)
            ci = self.wait_for_ci(gh, repo, latest.head_sha)
            if ci in ("green", "none"):
                self.mark_ready_checked(gh, repo, latest, ref, ci)

    def merge_pass(self) -> None:
        """Close the loop on merged PRs. The window is wide; the finished-state guard in merge_one
        keeps it idempotent."""
        since = f"{(self.clock().astimezone(UTC) - MERGED_WINDOW).date()}T00:00:00Z"
        for repo in self._open:
            gh = self.github_factory(repo)
            try:
                merged = gh.merged_prs_since(repo, self.cfg.identities.worker_github_login, since)
            except Exception as e:
                self.log(f"merge pass: could not list merged PRs on {repo}: {type(e).__name__}: {e}")
                continue
            for pr in merged:
                ref = self.pr_ref(pr)
                if not ref:
                    continue
                self._merged.setdefault(ref, pr)
                try:
                    self.merge_one(pr, ref)
                except Exception as e:
                    self.errored(ref, e)

    def merge_one(self, pr: PR, ref: str) -> None:
        try:
            item = self.plane.get_item(ref)
        except ValueError as e:
            self.log(f"skip merged PR #{pr.number}: {e}")
            return
        if item.state.casefold() in FINISHED_STATES:
            return
        if self.dry_run:
            self.report.planned.append(f"merged {ref} -> close loop")
            return
        project = self.projects.get(split_ref(ref)[0])
        target = "Ready for test" if project and project.deploys else "Done"
        sha = f" as {pr.merge_commit_sha[:7]}" if pr.merge_commit_sha else ""
        self.plane.set_state(ref, target)
        self.plane.comment(ref, f"[bau] PR #{pr.number} merged at {pr.merged_at}{sha}; moved to {target}.")
        self.repos.delete_checkout(pr.head_ref)

    def new_tickets_pass(self) -> None:
        candidates: list[Item] = []
        for ident in self.projects:
            candidates += self.plane.list_items(ident, "BAU")
        open_refs = {r for prs in self._open.values() for p in prs if (r := self.ref_of(p))}
        order = {"urgent": 0, "high": 1, "medium": 2, "low": 3, None: 4, "none": 4}
        for item in sorted(candidates, key=lambda i: (order.get(i.priority, 4), i.created_at)):
            if self.report.stop_reason != "ok":
                return
            if "blocked" in item.labels or item.ref in open_refs or item.state.casefold() in FINISHED_STATES:
                continue
            try:
                if not self.new_ticket_one(item):
                    return
            except Exception as e:
                self.errored(item.ref, e)

    def failed_attempts(self, ref: str) -> tuple[list[Comment], list[Comment]]:
        """The worker's attempt-ending comments since the owner last commented, and every comment
        since then. An owner comment re-opens the ticket to new attempts."""
        ids = self.cfg.identities
        comments = self.plane.comments(ref)
        start = 0
        for i, c in enumerate(comments):
            if c.author_email == ids.owner_plane_email:
                start = i + 1
        recent = comments[start:]
        failures = [
            c
            for c in recent
            if c.author_email == ids.worker_plane_email and c.text.startswith(FAILED_ATTEMPT_PREFIXES)
        ]
        return failures, recent

    def new_ticket_one(self, item: Item) -> bool:
        """Handle one candidate. Returns False when the night must stop."""
        merged = self._merged.get(item.ref)
        if merged is not None:
            self.log(f"{item.ref}: PR #{merged.number} is already merged; closing the loop instead")
            self.merge_one(merged, item.ref)
            return True
        failures, recent = self.failed_attempts(item.ref)
        if len(failures) >= MAX_FAILED_ATTEMPTS:
            self.decline(item, TRIED_TWICE, seen=recent)
            return True
        previous = failures[-1].text if failures else None
        known = {p.repo for p in self.projects.values()}
        repo = repo_for(item, self.projects)
        if not repo:
            self.decline(item, "no repository mapped for this project; add it to projects.yaml")
            return True
        if repo not in known:
            self.decline(item, f"repo override {repo} is not in projects.yaml")
            return True
        if not item.description.strip():
            self.decline(item, "ticket has no description; BAU needs one to work from")
            return True
        try:
            gh = self.github_factory(repo)
            protected = gh.branch_protected(repo)
        except Exception as e:  # an unreachable or uninstalled repo must not end the night
            self.decline(item, f"could not check branch protection on {repo} ({type(e).__name__})")
            return True
        if not protected:
            self.decline(item, f"{repo} has no branch protection on main; BAU refuses to work there")
            return True
        model = model_for(item, self.cfg.models)
        if self.dry_run:
            self.report.planned.append(f"new-ticket {item.ref} -> {repo} ({model})")
            return True
        if not self.may_start(new_ticket=True):
            return False
        self.work_new_ticket(item, repo, gh, model, previous)
        return self.report.stop_reason == "ok"

    def close_too_big(
        self, item: Item, repo: str, gh: GitHubClient, opened: PR, branch: str, back: str, detail: str
    ) -> None:
        gh.comment(repo, opened.number, f"[bau] Closing: {detail} exceeds the BAU size limit.")
        gh.close_pr(repo, opened.number)
        self.plane.set_state(item.ref, back)
        self.plane.comment(item.ref, f"[bau] Too big for BAU ({detail}). Returned to {back}.")
        self.repos.delete_checkout(branch)

    def reject_if_too_big(
        self, item: Item, repo: str, gh: GitHubClient, opened: PR, branch: str, back: str
    ) -> bool:
        files, lines = opened.changed_files, opened.additions + opened.deletions
        if not diff_too_big(files, lines, self.cfg.limits):
            return False
        self.close_too_big(item, repo, gh, opened, branch, back, f"{files} files / {lines} lines")
        return True

    def work_new_ticket(
        self, item: Item, repo: str, gh: GitHubClient, model: str, previous: str | None = None
    ) -> None:
        branch = branch_name(item.ref, item.name)
        back = item.state
        if back.casefold() == "in progress":  # picked up on an earlier night: restore the original state
            back = (
                previous_state(self.plane.comments(item.ref), self.cfg.identities.worker_plane_email) or back
            )
        self.plane.set_state(item.ref, "In progress")
        self.plane.assign(item.ref, self.cfg.identities.worker_plane_email)
        if not self.already_picked_up(item.ref, branch, back):
            self.plane.comment(
                item.ref, f"[bau:picked-up from={back}] Working on branch `{branch}` in {repo}."
            )
        try:
            self.work_picked_up(item, repo, gh, model, branch, back, previous)
        except Exception:
            try:  # leave the ticket where it was so the next night retries it cleanly
                self.plane.set_state(item.ref, back)
            except Exception as inner:  # noqa: BLE001 - best effort only
                self.log(f"{item.ref}: could not restore state {back}: {inner}")
            raise

    def already_picked_up(self, ref: str, branch: str, back: str) -> bool:
        worker = self.cfg.identities.worker_plane_email
        mine = [
            c
            for c in self.plane.comments(ref)
            if c.author_email == worker and "[bau] Error while working this ticket" not in c.text
        ]
        return (
            bool(mine) and f"[bau:picked-up from={back}]" in mine[-1].text and f"`{branch}`" in mine[-1].text
        )

    def work_picked_up(
        self,
        item: Item,
        repo: str,
        gh: GitHubClient,
        model: str,
        branch: str,
        back: str,
        previous: str | None = None,
    ) -> None:
        if self.repos.has_checkout(branch):  # left by a failed earlier night: start afresh from main
            self.repos.delete_checkout(branch)
        self.repos.create_checkout(repo, branch)
        result = self.session(
            item=item,
            repo=repo,
            branch=branch,
            brief=self.brief(item, repo, branch, None, None, previous),
            model=model,
        )
        if result.outcome == "too_big":
            self.plane.set_state(item.ref, back)
            self.plane.comment(item.ref, f"[bau] Too big for BAU: {result.reason}. Returned to {back}.")
            self.repos.delete_checkout(branch)
            return
        if result.outcome != "done":
            self.plane.comment(item.ref, f"[bau] Session ended with {result.outcome}: {result.reason}")
            return
        opened = next(
            (p for p in gh.open_prs(repo, self.cfg.identities.worker_github_login) if p.head_ref == branch),
            None,
        )
        if not opened:
            self.plane.comment(item.ref, "[bau] Session finished but no PR was opened; see the log.")
            return
        if self.reject_if_too_big(item, repo, gh, opened, branch, back):
            return
        self.report.prs_opened += 1
        self._open_total += 1
        self.plane.link(item.ref, opened.url, f"PR #{opened.number}")
        ci = self.wait_for_ci(gh, repo, opened.head_sha)
        if ci == "red" and self.may_start(new_ticket=False):
            ci, opened = self.fix_red_ci(item, repo, gh, model, opened, branch, back)
            if ci is None:
                return
        if ci == "green":
            self.mark_ready(gh, repo, opened, ci)
            self.plane.comment(item.ref, f"[bau] PR #{opened.number} opened and CI is green: {opened.url}")
        elif ci == "none":
            self.mark_ready(gh, repo, opened, ci)
            self.plane.comment(
                item.ref,
                f"[bau] PR #{opened.number} opened; no CI checks ran, so it is marked ready: {opened.url}",
            )
        else:
            self.plane.comment(item.ref, f"[bau] PR #{opened.number} left as draft; CI is {ci}: {opened.url}")

    def fix_red_ci(
        self, item: Item, repo: str, gh: GitHubClient, model: str, opened: PR, branch: str, back: str
    ) -> tuple[str | None, PR]:
        """One fix session for a PR that has just gone red. Returns (ci status, refreshed PR);
        status is None when the outcome has already been handled and nothing more should be done."""
        limit = self.cfg.limits.ci_fix_sessions_per_pr
        so_far = sum(CI_FIX_MARK in b for b in gh.issue_comments(repo, opened.number))
        log = gh.check_summaries(repo, opened.head_sha)
        result = self.session(
            item=item,
            repo=repo,
            branch=branch,
            brief=self.brief(item, repo, branch, Feedback(ci_red=True), log),
            model=model,
        )
        if result.outcome == "rate_limited":
            return None, opened
        if result.outcome == "too_big":
            self.close_too_big(item, repo, gh, opened, branch, back, f"fix session: {result.reason}")
            return None, opened
        gh.comment(repo, opened.number, f"{CI_FIX_MARK} {so_far + 1}/{limit}")
        if result.outcome != "done":
            gh.comment(
                repo, opened.number, f"[bau] CI-fix session ended with {result.outcome}: {result.reason}"
            )
            self.plane.comment(
                item.ref,
                f"[bau] PR #{opened.number} left as draft; the CI-fix session ended with {result.outcome}.",
            )
            return None, opened
        opened = gh.pr(repo, opened.number)
        if self.reject_if_too_big(item, repo, gh, opened, branch, back):
            return None, opened
        return self.wait_for_ci(gh, repo, opened.head_sha), opened
