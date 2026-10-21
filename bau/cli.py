from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bau import version as build
from bau.abandon import (
    RUN_IN_PROGRESS,
    AlreadyAbandoned,
    InvalidRef,
    RunInProgress,
    UnknownProject,
    abandon,
)
from bau.atc import AtcClient, AtcState, AtcUnavailable, load_atc_state
from bau.checkout import Repos
from bau.config import Config, KeychainRef, bau_home, load_config, paused_reader
from bau.github import GitHubApp, GitHubClient
from bau.lock import run_lock
from bau.markers import latest_asked
from bau.plane import PlaneClient
from bau.runloop import TOKEN_REFRESH_MARGIN, Night, NightReport, repo_for
from bau.secrets import read_secret
from bau.session import SessionRunner
from bau.snapshot import (
    DeployRow,
    LastRun,
    OpenPrRow,
    QuestionRow,
    build_snapshot,
    drop_open_pr,
    read_snapshot,
    write_snapshot,
)

# Stop reasons that make up a normal night; anything else (or an exception) exits non-zero.
NORMAL_STOP_REASONS = frozenset({"ok", "cutoff", "pace", "paused", "five-hour", "locked"})
# How long each job waits for the shared run lock before giving up (seconds).
RUN_LOCK_WAIT = 3600


def exit_code(stop_reason: str) -> int:
    return 0 if stop_reason in NORMAL_STOP_REASONS else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bau")
    p.add_argument("--config", type=Path, default=bau_home() / "config")
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
    sub.add_parser("version", help="print the build version", parents=[common])
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


ATC_STOP = "preflight: atc"


def wire(cfg: Config, config_dir: Path):
    """Build the clients. Projects and priority come from ATC (or its cache on Cere); raises
    AtcUnavailable when neither can be read, before any other service is touched."""
    atc_state = load_atc_state(
        AtcClient(cfg.atc.url, cfg.plane.client_cert, cfg.plane.client_key), cfg.paths.atc_cache
    )
    return wire_services(cfg, atc_state)


def wire_services(cfg: Config, atc_state: AtcState):
    """The clients around an ATC state already in hand (wire()'s second half)."""
    projects = atc_state.projects
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
    return projects, plane, app, factory, repos, clock, atc_state


def _kept_questions(previous: list[dict], ident: str) -> list[QuestionRow]:
    known = {f.name for f in fields(QuestionRow)}
    kept = []
    for r in previous:
        if isinstance(r, dict) and str(r.get("ref", "")).rsplit("-", 1)[0] == ident:
            try:
                kept.append(QuestionRow(**{k: v for k, v in r.items() if k in known}))
            except TypeError:
                continue
    return kept


def snapshot_rows(
    cfg: Config,
    projects,
    plane,
    factory,
    clock,
    log: Callable[..., None] = print,
    previous_questions: list[dict] | None = None,
):
    open_rows: list[OpenPrRow] = []
    deploy_rows: list[DeployRow] = []
    question_rows: list[QuestionRow] = []
    now = clock()
    for ident, project in projects.items():
        try:
            question_rows.extend(_question_rows(cfg, ident, projects, plane, now, log))
        except Exception as e:
            log(f"snapshot: keeping the previous questions for {ident}: {e}")
            question_rows.extend(_kept_questions(previous_questions or [], ident))
        if project.repo is None:
            continue
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
    return open_rows, deploy_rows, question_rows


def _question_rows(cfg: Config, ident: str, projects, plane, now: datetime, log) -> list[QuestionRow]:
    rows = []
    for item in plane.list_items(ident, "bau-questions"):
        asked = latest_asked(plane.comments(item.ref), cfg.identities.worker_plane_email)
        if asked is None:
            log(f"snapshot: {item.ref} is labelled bau-questions but has no parsable questions")
            continue
        repo = repo_for(item, projects) or ""
        at = datetime.fromisoformat(asked.at.replace("Z", "+00:00"))
        rows.append(
            QuestionRow(
                repo,
                item.ref,
                item.name,
                plane.project_url(item.ref),
                f"https://github.com/{repo}" if repo else "",
                asked.at,
                (now - at).days,
                asked.round,
                asked.summary,
                [asdict(q) for q in asked.questions],
            )
        )
    return rows


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
    projects, plane, app, factory, repos, clock, atc_state = wired
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
        priority=atc_state.priority,
        atc=atc_label(atc_state),
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
    projects, plane, _, factory, _, clock, atc_state = wired
    fetched_at, age = _stale_age(atc_state, clock())
    last = LastRun(
        started.isoformat(),
        clock().isoformat(),
        report.sessions,
        report.prs_opened,
        report.feedback_handled,
        stop_reason,
        report.telemetry,
        report.swept,
        report.labelled_bau,
        report.labelled_not_bau,
        report.triaged,
        report.asked,
        report.atc,
        fetched_at,
        age,
    )
    previous = read_snapshot(cfg.paths.snapshot) or {}
    open_rows, deploy_rows, question_rows = (
        ([], [], [])
        if failed
        else snapshot_rows(cfg, projects, plane, factory, clock, previous_questions=previous.get("questions"))
    )
    data = build_snapshot(
        paused=paused,
        last_run=last,
        latest=report.latest,
        open_prs=open_rows,
        deploy_pending=deploy_rows,
        declined=report.declined,
        questions=question_rows,
        nudge_thresholds=cfg.nudge,
        uninstalled=report.uninstalled,
        now=time.time(),
        pace_override=previous.get("pace"),
    )
    if failed:  # the services may be what failed: keep the last known rows rather than blanking them
        data["open_prs"] = previous.get("open_prs", [])
        data["deploy_pending"] = previous.get("deploy_pending", [])
        data["questions"] = previous.get("questions", [])
        data["nudge"] = previous.get("nudge", data["nudge"])  # computed from the kept rows, not the blanks
    write_snapshot(cfg.paths.snapshot, data)


def atc_label(state: AtcState) -> str:
    return "stale" if state.stale else "empty" if state.empty else "ok"


def _stale_age(state: AtcState, now: datetime) -> tuple[str, float | None]:
    """When the night ran on the cache: when it was fetched and its age in hours (one decimal)."""
    if not state.stale or not state.fetched_at:
        return "", None
    try:
        fetched = datetime.fromisoformat(state.fetched_at)
    except ValueError:
        return state.fetched_at, None
    return state.fetched_at, round((now - fetched).total_seconds() / 3600, 1)


def _unwired(cfg: Config):
    """Stands in for wire() when ATC and its cache are both unreadable: a clock and nothing else,
    enough for a failure snapshot that touches no service."""
    tz = ZoneInfo(cfg.schedule.timezone)
    return {}, None, None, None, None, lambda: datetime.now(tz), AtcState({}, {}, True)


def _run(args: argparse.Namespace, cfg: Config) -> int:
    paused = paused_reader(cfg.paths.pause_file)
    try:
        wired = wire(cfg, args.config)
    except AtcUnavailable as e:
        print(f"bau run: {ATC_STOP}: {e} (and no cached copy)")
        if not args.dry_run:
            unwired = _unwired(cfg)
            _write_run_snapshot(cfg, unwired, unwired[5](), NightReport(), ATC_STOP, paused(), failed=True)
        return exit_code(ATC_STOP)
    clock = wired[5]
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


def _locked_out(cfg: Config, args: argparse.Namespace) -> int:
    print("bau run: another BAU operation held the lock for an hour; giving up")
    if args.dry_run:
        return 0
    try:
        wired = wire(cfg, args.config)
    except AtcUnavailable:
        wired = _unwired(cfg)
    clock = wired[5]
    paused = paused_reader(cfg.paths.pause_file)
    # failed=True keeps the last known rows and touches no service: nothing ran tonight.
    _write_run_snapshot(cfg, wired, clock(), NightReport(), "locked", paused(), failed=True)
    return 0


def _version() -> int:
    print(f"bau {build.VERSION} ({build.GIT_SHA or 'unstamped'})")
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "version":  # before load_config: needs no config file
        return _version()
    cfg = load_config(args.config)
    if args.command == "run":
        cfg.paths.lock.parent.mkdir(parents=True, exist_ok=True)
        cfg.paths.logs.mkdir(parents=True, exist_ok=True)
        with run_lock(cfg.paths.lock, RUN_LOCK_WAIT) as held:
            if not held:
                return _locked_out(cfg, args)
            return _run(args, cfg)
    try:
        wired = wire(cfg, args.config)
    except AtcUnavailable as e:
        if args.command != "serve":
            print(f"bau {args.command}: {ATC_STOP}: {e} (and no cached copy); nothing done", file=sys.stderr)
            return exit_code(ATC_STOP)
        # The dashboard still serves the snapshot; writes that need a project refuse until a
        # restart after ATC (or its cache) is back. launchd's KeepAlive would otherwise respawn it.
        print(
            f"bau serve: {ATC_STOP}: {e} (and no cached copy); serving with no projects",
            file=sys.stderr,
        )
        wired = wire_services(cfg, AtcState({}, {}, True))
    projects, plane, app, factory, repos, clock, _ = wired
    if args.command == "probe":
        paused = paused_reader(cfg.paths.pause_file)
        print(_night(args, cfg, wired, paused).probe())
        return 0
    if args.command == "snapshot":
        previous = read_snapshot(cfg.paths.snapshot) or {}
        open_rows, deploy_rows, question_rows = snapshot_rows(
            cfg, projects, plane, factory, clock, previous_questions=previous.get("questions")
        )
        last = _last_run(previous.get("last_run"))
        write_snapshot(
            cfg.paths.snapshot,
            build_snapshot(
                paused=paused_reader(cfg.paths.pause_file)(),
                last_run=last,
                latest=None,
                open_prs=open_rows,
                deploy_pending=deploy_rows,
                declined=previous.get("declined", []),
                questions=question_rows,
                nudge_thresholds=cfg.nudge,
                uninstalled=previous.get("uninstalled", []),
                now=time.time(),
                pace_override=previous.get("pace"),
            ),
        )
        return 0
    if args.command == "serve":
        from bau.api import create_app, serve, static_dir

        token = read_secret(KeychainRef("bau", "proxy-token"))
        serve(create_app(cfg, projects, plane, factory, repos, static_dir(), token), args.port)
        return 0
    if args.command == "abandon":
        return _abandon(args, cfg, wired)
    return 2


def _abandon(args: argparse.Namespace, cfg: Config, wired) -> int:
    projects, plane, _, factory, repos, _, _ = wired
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
            lock_path=cfg.paths.lock,
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
    try:
        drop_open_pr(cfg.paths.snapshot, r.ref)
    except Exception as e:  # noqa: BLE001 - the abandon succeeded; the next snapshot corrects this
        print(f"warning: could not update the snapshot: {type(e).__name__}", file=sys.stderr)
    print(f"{r.ref}: PR {r.pr}, restored to {r.restored_state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
