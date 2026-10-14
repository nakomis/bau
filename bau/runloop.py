from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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


def previous_state(comments: list[Comment]) -> str | None:
    for c in reversed(comments):
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
    ):
        self.cfg, self.projects, self.plane, self.gh_app = cfg, projects, plane, gh_app
        self.github_factory, self.repos, self.runner, self.clock = github_factory, repos, runner, clock
        self.oauth_token, self.dry_run, self.log, self.sleep = oauth_token, dry_run, log, sleep
        self.policy = Policy(cfg.limits, cfg.schedule, clock)
        self.report = NightReport()
        self._tokens: dict[str, Token] = {}
        self._open: dict[str, list[PR]] = {}
        self._open_total = 0

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def ref_of(pr: PR) -> str | None:
        m = REF_IN_BODY.search(pr.body or "") or REF_IN_TITLE.search(pr.title or "")
        return m.group(1) if m else None

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
        try:
            proc = subprocess.run(
                cmd,
                input="",
                capture_output=True,
                text=True,
                env={
                    "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token,
                    "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
                    "HOME": str(Path.home()),
                },
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        latest = None
        for line in proc.stdout.splitlines():
            obj = parse_stream_line(line)
            if obj and (rl := rate_limit_from_event(obj)):
                latest = rl
        return latest

    def wait_for_ci(
        self, gh: GitHubClient, repo: str, sha: str, timeout_s: float = 1800, poll_s: float = 30
    ) -> str:
        status = gh.ci_status(repo, sha)
        waited = 0.0
        while status in ("pending", "none") and waited < timeout_s:
            self.sleep(poll_s)
            waited += poll_s
            status = gh.ci_status(repo, sha)
        return status

    def decline(self, item: Item, reason: str) -> None:
        self.report.declined.append({"ref": item.ref, "reason": reason, "at": self.now_iso()})
        self.log(f"decline {item.ref}: {reason}")
        if self.dry_run:
            return
        text = f"[bau] Not picked up: {reason}"
        if not any(text in c.text for c in self.plane.comments(item.ref)):
            self.plane.comment(item.ref, text)

    def may_start(self, *, new_ticket: bool) -> bool:
        d = self.policy.can_start_session(
            latest=self.report.latest,
            open_prs=self._open_total,
            sessions_started=self.report.sessions,
            paused=self.cfg.paused,
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
        if result.latest:
            self.report.latest = result.latest
        if result.outcome == "rate_limited":
            self.report.stop_reason = "rejected"
        return result

    def brief(self, item: Item, repo: str, branch: str, feedback: Feedback | None, ci_log: str | None) -> str:
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
        )

    # -- passes --------------------------------------------------------------
    def run(self) -> NightReport:
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
        if not self.may_start(new_ticket=False):
            return self.report
        self.feedback_pass()
        self.merge_pass()
        self.new_tickets_pass()
        return self.report

    def feedback_pass(self) -> None:
        limit = self.cfg.limits.ci_fix_sessions_per_pr
        ids = self.cfg.identities
        for repo, prs in self._open.items():
            gh = self.github_factory(repo)
            for pr in sorted(prs, key=lambda p: p.created_at):
                ref = self.ref_of(pr)
                if not ref:
                    continue
                threads = pending_threads(
                    gh.review_threads(repo, pr.number), ids.github_login, ids.worker_github_login
                )
                plane_comments = pending_plane_comments(
                    self.plane.comments(ref), ids.owner_plane_email, ids.worker_plane_email
                )
                ci_red, fixes_so_far = False, 0
                if gh.ci_status(repo, pr.head_sha) == "red":
                    bodies = gh.issue_comments(repo, pr.number)
                    fixes_so_far = sum(CI_FIX_MARK in b for b in bodies)
                    ci_red = fixes_so_far < limit
                    if not ci_red and not self.dry_run:
                        giving_up = f"[bau] CI still red after {limit} fix sessions; leaving this for Martin."
                        if not any(giving_up in b for b in bodies):
                            gh.comment(repo, pr.number, giving_up)
                fb = Feedback(threads, plane_comments, conflict=pr.mergeable_state == "dirty", ci_red=ci_red)
                if fb.empty:
                    continue
                if self.dry_run:
                    self.report.planned.append(f"feedback {ref} -> {repo} PR #{pr.number}")
                    continue
                if not self.may_start(new_ticket=False):
                    return
                try:
                    item = self.plane.get_item(ref)
                except ValueError as e:
                    self.log(f"skip PR #{pr.number}: {e}")
                    continue
                if not fb.conflict and not self.repos.rebase_onto_main(pr.head_ref, self.token(repo)):
                    fb = replace(fb, conflict=True)
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
                if result.outcome == "done":
                    self.report.feedback_handled += 1
                    for c in plane_comments:
                        self.plane.comment(ref, f"[bau] Addressed in PR #{pr.number}. {ack_marker(c.id)}")
                else:
                    gh.comment(
                        repo,
                        pr.number,
                        f"[bau] Feedback session ended with {result.outcome}: {result.reason}",
                    )
                if self.report.stop_reason != "ok":
                    return

    def merge_pass(self) -> None:
        since = f"{(self.clock().astimezone(UTC) - timedelta(days=1)).date()}T00:00:00Z"
        for repo in self._open:
            gh = self.github_factory(repo)
            for pr in gh.merged_prs_since(repo, self.cfg.identities.worker_github_login, since):
                ref = self.ref_of(pr)
                if not ref:
                    continue
                try:
                    item = self.plane.get_item(ref)
                except ValueError as e:
                    self.log(f"skip merged PR #{pr.number}: {e}")
                    continue
                if item.state.casefold() in FINISHED_STATES:
                    continue
                if self.dry_run:
                    self.report.planned.append(f"merged {ref} -> close loop")
                    continue
                project = self.projects.get(split_ref(ref)[0])
                target = "Ready for test" if project and project.deploys else "Done"
                self.plane.set_state(ref, target)
                self.plane.comment(ref, f"[bau] PR #{pr.number} merged at {pr.merged_at}; moved to {target}.")
                self.repos.delete_checkout(pr.head_ref)

    def new_tickets_pass(self) -> None:
        candidates: list[Item] = []
        for ident in self.projects:
            candidates += self.plane.list_items(ident, "BAU")
        open_refs = {r for prs in self._open.values() for p in prs if (r := self.ref_of(p))}
        order = {"urgent": 0, "high": 1, "medium": 2, "low": 3, None: 4, "none": 4}
        for item in sorted(candidates, key=lambda i: (order.get(i.priority, 4), i.created_at)):
            if "blocked" in item.labels or item.ref in open_refs or item.state.casefold() in FINISHED_STATES:
                continue
            repo = repo_for(item, self.projects)
            if not repo:
                self.decline(item, "no repository mapped for this project; add it to projects.yaml")
                continue
            if not item.description.strip():
                self.decline(item, "ticket has no description; BAU needs one to work from")
                continue
            try:
                gh = self.github_factory(repo)
                protected = gh.branch_protected(repo)
            except Exception as e:  # an unreachable or uninstalled repo must not end the night
                self.decline(item, f"could not check branch protection on {repo} ({type(e).__name__})")
                continue
            if not protected:
                self.decline(item, f"{repo} has no branch protection on main; BAU refuses to work there")
                continue
            model = model_for(item, self.cfg.models)
            if self.dry_run:
                self.report.planned.append(f"new-ticket {item.ref} -> {repo} ({model})")
                continue
            if not self.may_start(new_ticket=True):
                return
            self.work_new_ticket(item, repo, gh, model)
            if self.report.stop_reason != "ok":
                return

    def reject_if_too_big(
        self, item: Item, repo: str, gh: GitHubClient, opened: PR, branch: str, back: str
    ) -> bool:
        files, lines = self.repos.diff_stats(branch)
        if not diff_too_big(files, lines, self.cfg.limits):
            return False
        gh.comment(
            repo, opened.number, f"[bau] Closing: {files} files / {lines} lines exceeds the BAU size limit."
        )
        gh.close_pr(repo, opened.number)
        self.plane.set_state(item.ref, back)
        self.plane.comment(
            item.ref, f"[bau] Too big for BAU ({files} files, {lines} lines). Returned to {back}."
        )
        self.repos.delete_checkout(branch)
        return True

    def work_new_ticket(self, item: Item, repo: str, gh: GitHubClient, model: str) -> None:
        branch = branch_name(item.ref, item.name)
        back = item.state
        if back.casefold() == "in progress":  # picked up on an earlier night: restore the original state
            back = previous_state(self.plane.comments(item.ref)) or back
        self.plane.set_state(item.ref, "In progress")
        self.plane.assign(item.ref, self.cfg.identities.worker_plane_email)
        self.plane.comment(item.ref, f"[bau:picked-up from={back}] Working on branch `{branch}` in {repo}.")
        self.repos.create_checkout(repo, branch)
        result = self.session(
            item=item, repo=repo, branch=branch, brief=self.brief(item, repo, branch, None, None), model=model
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
            ci, opened = self.fix_red_ci(item, repo, gh, model, opened, branch)
            if ci is None:
                self.plane.set_state(item.ref, back)
                return
        if ci == "green":
            gh.mark_ready(opened.node_id)
            self.plane.comment(item.ref, f"[bau] PR #{opened.number} opened and CI is green: {opened.url}")
        else:
            self.plane.comment(item.ref, f"[bau] PR #{opened.number} left as draft; CI is {ci}: {opened.url}")

    def fix_red_ci(
        self, item: Item, repo: str, gh: GitHubClient, model: str, opened: PR, branch: str
    ) -> tuple[str | None, PR]:
        """One fix session for a PR that has just gone red. Returns (ci status, refreshed PR);
        status is None if the fix pushed the PR over the size limit and it was closed."""
        limit = self.cfg.limits.ci_fix_sessions_per_pr
        so_far = sum(CI_FIX_MARK in b for b in gh.issue_comments(repo, opened.number))
        log = gh.check_summaries(repo, opened.head_sha)
        self.session(
            item=item,
            repo=repo,
            branch=branch,
            brief=self.brief(item, repo, branch, Feedback(ci_red=True), log),
            model=model,
        )
        gh.comment(repo, opened.number, f"{CI_FIX_MARK} {so_far + 1}/{limit}")
        opened = gh.pr(repo, opened.number)
        if self.reject_if_too_big(item, repo, gh, opened, branch, item.state):
            return None, opened
        return self.wait_for_ci(gh, repo, opened.head_sha), opened
