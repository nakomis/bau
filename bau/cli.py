from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bau.abandon import (
    RUN_IN_PROGRESS,
    AlreadyAbandoned,
    InvalidRef,
    RunInProgress,
    UnknownProject,
    abandon,
)
from bau.checkout import Repos
from bau.config import Config, KeychainRef, load_config, load_projects, paused_reader
from bau.github import GitHubApp, GitHubClient
from bau.lock import run_lock
from bau.plane import PlaneClient
from bau.runloop import TOKEN_REFRESH_MARGIN, Night, NightReport
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
    sv = sub.add_parser("serve", help="the dashboard API and static site", parents=[common])
    sv.add_argument("--port", type=int, default=8800)
    ab = sub.add_parser("abandon", help="abandon a picked-up item", parents=[common])
    ab.add_argument("ref")
    ab.add_argument("--note", default="")
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


def _last_run(raw: dict | None) -> LastRun | None:
    if not raw:
        return None
    known = {f.name for f in fields(LastRun)}
    return LastRun(**{k: v for k, v in raw.items() if k in known})


def _on_sigterm(signum, frame):
    # SystemExit unwinds through the session runner's cleanup, which kills the container.
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return _dispatch(args)
    except Exception as e:
        print(f"bau {args.command} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


def _night(args: argparse.Namespace, cfg: Config, wired, paused: Callable[[], bool]) -> Night:
    projects, plane, app, factory, repos, clock = wired
    return Night(
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
        paused=paused,
    )


def _write_run_snapshot(
    cfg: Config,
    wired,
    started: datetime,
    report: NightReport,
    stop_reason: str,
    paused: bool,
    *,
    failed: bool,
) -> None:
    projects, plane, _, factory, _, clock = wired
    last = LastRun(
        started.isoformat(),
        clock().isoformat(),
        report.sessions,
        report.prs_opened,
        report.feedback_handled,
        stop_reason,
        report.telemetry,
    )
    previous = read_snapshot(cfg.paths.snapshot) or {}
    open_rows, deploy_rows = ([], []) if failed else snapshot_rows(cfg, projects, plane, factory, clock)
    data = build_snapshot(
        paused=paused,
        last_run=last,
        latest=report.latest,
        open_prs=open_rows,
        deploy_pending=deploy_rows,
        declined=report.declined,
        now=time.time(),
        pace_override=previous.get("pace"),
    )
    if failed:  # the services may be what failed: keep the last known rows rather than blanking them
        data["open_prs"] = previous.get("open_prs", [])
        data["deploy_pending"] = previous.get("deploy_pending", [])
    write_snapshot(cfg.paths.snapshot, data)


def _run(args: argparse.Namespace, cfg: Config) -> int:
    wired = wire(cfg, args.config)
    clock = wired[5]
    paused = paused_reader(args.config / "bau.yaml", cfg.paused)
    night = _night(args, cfg, wired, paused)
    started = clock()
    try:
        report = night.run()
    except BaseException as e:
        if args.dry_run:
            raise
        reason = f"failed: {type(e).__name__}: {e}"[:200]
        try:
            _write_run_snapshot(cfg, wired, started, night.report, reason, paused(), failed=True)
        except Exception as inner:  # noqa: BLE001 - the original failure matters more
            print(f"bau run: could not write the failure snapshot: {inner}", file=sys.stderr)
        if isinstance(e, Exception):
            print(f"bau run failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        raise
    if args.dry_run:
        print("\n".join(report.planned) or "nothing to do")
        return 0
    _write_run_snapshot(cfg, wired, started, report, report.stop_reason, paused(), failed=False)
    return exit_code(report.stop_reason)


def _dispatch(args: argparse.Namespace) -> int:
    cfg = load_config(args.config / "bau.yaml")
    if args.command == "run":
        with run_lock(cfg.paths.logs.parent / "run.lock") as held:
            if not held:
                print("another bau run is in progress")
                return 0
            return _run(args, cfg)
    wired = wire(cfg, args.config)
    projects, plane, app, factory, repos, clock = wired
    if args.command == "probe":
        print(_night(args, cfg, wired, lambda: cfg.paused).probe())
        return 0
    if args.command == "snapshot":
        previous = read_snapshot(cfg.paths.snapshot) or {}
        open_rows, deploy_rows = snapshot_rows(cfg, projects, plane, factory, clock)
        last = _last_run(previous.get("last_run"))
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
    if args.command == "serve":
        from bau.api import create_app, serve

        static_dir = Path(args.config).parent / "web" / "dist"
        token = read_secret(KeychainRef("bau", "proxy-token"))
        serve(create_app(cfg, projects, plane, factory, repos, static_dir, token), args.port)
        return 0
    if args.command == "abandon":
        return _abandon(args, cfg, wired)
    return 2


def _abandon(args: argparse.Namespace, cfg: Config, wired) -> int:
    projects, plane, _, factory, repos, _ = wired
    try:
        r = abandon(
            args.ref,
            plane=plane,
            github_factory=factory,
            repos=repos,
            projects=projects,
            worker_login=cfg.identities.worker_github_login,
            worker_email=cfg.identities.worker_plane_email,
            note=args.note,
            lock_path=cfg.paths.logs.parent / "run.lock",
        )
    except (InvalidRef, UnknownProject) as e:
        print(e)
        return 2
    except RunInProgress:
        print(RUN_IN_PROGRESS)
        return 1
    except AlreadyAbandoned as e:
        print(e)
        return 1
    print(f"{r.ref}: PR {r.pr}, restored to {r.restored_state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
