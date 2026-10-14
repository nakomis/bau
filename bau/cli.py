from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bau.checkout import Repos
from bau.config import Config, load_config, load_projects
from bau.github import GitHubApp, GitHubClient
from bau.plane import PlaneClient
from bau.runloop import TOKEN_REFRESH_MARGIN, Night
from bau.secrets import read_secret
from bau.session import SessionRunner
from bau.snapshot import (
    DeployRow,
    LastRun,
    OpenPrRow,
    build_snapshot,
    read_snapshot,
    write_snapshot,
)

DEFAULT_CONFIG = Path("/Users/nakomis/bau/src/config")
# Stop reasons that make up a normal night; anything else (or an exception) exits non-zero.
NORMAL_STOP_REASONS = frozenset({"ok", "cutoff", "pace", "ceiling", "paused", "five-hour"})


def exit_code(stop_reason: str) -> int:
    return 0 if stop_reason in NORMAL_STOP_REASONS else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bau")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    # Also accepted after the subcommand; SUPPRESS keeps the top-level value when it is absent.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="the nightly run", parents=[common])
    run.add_argument("--dry-run", action="store_true")
    sub.add_parser("snapshot", help="refresh state.json without starting sessions", parents=[common])
    sub.add_parser("probe", help="print the current usage windows", parents=[common])
    return p


class ClientFactory:
    """One GitHub client per repo, re-minted when its installation token is near expiry."""

    def __init__(
        self,
        app: GitHubApp,
        clock: Callable[[], datetime],
        client_class: Callable[[str], GitHubClient] = GitHubClient,
    ):
        self.app, self.clock, self.client_class = app, clock, client_class
        self._cache: dict[str, tuple[datetime, GitHubClient]] = {}

    def __call__(self, repo: str) -> GitHubClient:
        cached = self._cache.get(repo)
        if cached is not None and cached[0] - TOKEN_REFRESH_MARGIN > self.clock():
            return cached[1]
        token = self.app.installation_token(repo)
        expires = datetime.fromisoformat(token.expires_at.replace("Z", "+00:00"))
        client = self.client_class(token.value)
        self._cache[repo] = (expires, client)
        return client


def wire(cfg: Config, config_dir: Path):
    projects = load_projects(config_dir / "projects.yaml")
    plane = PlaneClient(
        cfg.plane.url,
        cfg.plane.workspace,
        read_secret(cfg.plane.api_key),
        cfg.plane.client_cert,
        cfg.plane.client_key,
    )
    app = GitHubApp(
        cfg.github_app.app_id, read_secret(cfg.github_app.private_key), cfg.github_app.installation_id
    )
    tz = ZoneInfo(cfg.schedule.timezone)

    def clock() -> datetime:
        return datetime.now(tz)

    factory = ClientFactory(app, clock)
    repos = Repos(cfg.paths.repos, cfg.paths.work)
    return projects, plane, app, factory, repos, clock


def snapshot_rows(cfg: Config, projects, plane, factory, clock, log: Callable[..., None] = print):
    open_rows: list[OpenPrRow] = []
    deploy_rows: list[DeployRow] = []
    now = clock()
    for ident, project in projects.items():
        try:
            gh = factory(project.repo)
            rows = []
            for pr in gh.open_prs(project.repo, cfg.identities.worker_github_login):
                ref = Night.ref_of(pr)
                if not ref:
                    continue
                threads = gh.review_threads(project.repo, pr.number)
                unresolved = sum(1 for t in threads if not t.resolved)
                opened = datetime.fromisoformat(pr.created_at.replace("Z", "+00:00"))
                rows.append(
                    OpenPrRow(
                        project.repo,
                        ref,
                        pr.title,
                        pr.url,
                        pr.created_at,
                        (now - opened).days,
                        gh.ci_status(project.repo, pr.head_sha),
                        pr.draft,
                        unresolved,
                        plane.project_url(ref),
                    )
                )
            deploys = []
            if project.deploys:
                for item in plane.list_items(ident, "BAU"):
                    if item.state == "Ready for test":
                        deploys.append(
                            DeployRow(project.repo, item.ref, item.name, "", "", project.deploy_action)
                        )
        except Exception as e:
            log(f"snapshot: skipping {project.repo}: {e}")
            continue
        open_rows.extend(rows)
        deploy_rows.extend(deploys)
    return open_rows, deploy_rows


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except Exception as e:
        print(f"bau {args.command} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    cfg = load_config(args.config / "bau.yaml")
    projects, plane, app, factory, repos, clock = wire(cfg, args.config)
    if args.command in ("probe", "run"):
        night = Night(
            cfg,
            projects,
            plane,
            app,
            factory,
            repos,
            SessionRunner(cfg),
            clock,
            oauth_token=read_secret(cfg.claude.oauth_token),
            dry_run=getattr(args, "dry_run", False),
        )
        if args.command == "probe":
            print(night.probe())
            return 0
        started = clock()
        report = night.run()
        if args.dry_run:
            print("\n".join(report.planned) or "nothing to do")
            return 0
        last = LastRun(
            started.isoformat(),
            clock().isoformat(),
            report.sessions,
            report.prs_opened,
            report.feedback_handled,
            report.stop_reason,
            report.telemetry,
        )
        previous = read_snapshot(cfg.paths.snapshot) or {}
        open_rows, deploy_rows = snapshot_rows(cfg, projects, plane, factory, clock)
        write_snapshot(
            cfg.paths.snapshot,
            build_snapshot(
                paused=cfg.paused,
                last_run=last,
                latest=report.latest,
                open_prs=open_rows,
                deploy_pending=deploy_rows,
                declined=report.declined,
                now=time.time(),
                pace_override=previous.get("pace"),
            ),
        )
        return exit_code(report.stop_reason)
    if args.command == "snapshot":
        previous = read_snapshot(cfg.paths.snapshot) or {}
        open_rows, deploy_rows = snapshot_rows(cfg, projects, plane, factory, clock)
        last = LastRun(**previous["last_run"]) if previous.get("last_run") else None
        write_snapshot(
            cfg.paths.snapshot,
            build_snapshot(
                paused=cfg.paused,
                last_run=last,
                latest=None,
                open_prs=open_rows,
                deploy_pending=deploy_rows,
                declined=previous.get("declined", []),
                now=time.time(),
                pace_override=previous.get("pace"),
            ),
        )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
