from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from importlib import resources
from pathlib import Path

import yaml

from bau.version import image_tag


@dataclass(frozen=True)
class KeychainRef:
    service: str
    account: str


@dataclass(frozen=True)
class Limits:
    pace_margin: float
    five_hour_kill: float
    session_wall_clock_minutes: int
    session_budget_usd: float
    fail_closed_sessions: int
    ci_fix_sessions_per_pr: int
    too_big_files: int
    too_big_lines: int
    sweep_session_wall_clock_minutes: int
    triage_budget_usd: float
    triage_wall_clock_minutes: int
    max_question_rounds: int
    sweep_owner_quiet_days: int
    sweep_batch_items: int
    max_triage_failures: int


@dataclass(frozen=True)
class Nudge:
    prs_warn_count: int
    prs_warn_days: int
    prs_down_count: int
    prs_down_days: int
    questions_warn_count: int
    questions_warn_days: int
    questions_down_count: int
    questions_down_days: int


@dataclass(frozen=True)
class Schedule:
    no_new_sessions_after: time
    timezone: str


@dataclass(frozen=True)
class Models:
    default: str
    fable: str
    fable_label: str


@dataclass(frozen=True)
class Identities:
    github_login: str
    owner_plane_email: str
    worker_plane_email: str
    worker_github_login: str


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: int
    installation_id: int
    private_key: KeychainRef


@dataclass(frozen=True)
class PlaneConfig:
    url: str
    workspace: str
    api_key: KeychainRef
    client_cert: Path | None
    client_key: Path | None


@dataclass(frozen=True)
class AtcConfig:
    url: str


@dataclass(frozen=True)
class ClaudeConfig:
    oauth_token: KeychainRef


@dataclass(frozen=True)
class Paths:
    repos: Path
    work: Path
    logs: Path
    snapshot: Path
    pause_file: Path
    atc_cache: Path
    lock: Path


@dataclass(frozen=True)
class Container:
    image: str
    network: str
    proxy: str


@dataclass(frozen=True)
class Config:
    schedule: Schedule
    limits: Limits
    models: Models
    identities: Identities
    github_app: GitHubAppConfig
    plane: PlaneConfig
    claude: ClaudeConfig
    paths: Paths
    container: Container
    nudge: Nudge
    atc: AtcConfig


@dataclass(frozen=True)
class ProjectConfig:
    identifier: str
    repo: str | None
    deploys: bool
    deploy_action: str = ""


def _keychain(d: dict) -> KeychainRef:
    return KeychainRef(service=d["service"], account=d["account"])


def bau_home() -> Path:
    """`$BAU_HOME`, expanded, else the macOS application-support directory."""
    env = os.environ.get("BAU_HOME")
    return Path(env).expanduser() if env else Path("~/Library/Application Support/BAU").expanduser()


def _path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value.replace("${BAU_HOME}", str(bau_home()))).expanduser()


def _time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _merge(base: dict, over: dict) -> dict:
    """Dicts merge recursively; any other value in `over` replaces."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _default_image() -> str:
    tag = image_tag()
    return f"registry.home.nakomis.com/nakomis/bau-agent:{tag}" if tag else "bau-agent:latest"


def load_config(config_dir: Path) -> Config:
    """The packaged defaults, overlaid with `config_dir/host.yaml` when it exists."""
    raw = yaml.safe_load((resources.files("bau") / "defaults.yaml").read_text())
    host = Path(config_dir) / "host.yaml"
    if host.exists():
        overlay = yaml.safe_load(host.read_text())
        if overlay is None:
            overlay = {}
        if not isinstance(overlay, dict):
            raise ValueError(f"{host}: top level must be a mapping, not {type(overlay).__name__}")
        unknown = sorted(set(overlay) - set(raw))
        if unknown:
            raise ValueError(f"{host}: unknown top-level key(s) not in defaults.yaml: {', '.join(unknown)}")
        for key, value in overlay.items():
            if isinstance(raw[key], dict) and not isinstance(value, dict):
                raise ValueError(f"{host}: '{key}' must be a mapping, not {type(value).__name__}")
        raw = _merge(raw, overlay)
    raw["container"].setdefault("image", _default_image())
    return Config(
        schedule=Schedule(
            _time(raw["schedule"]["no_new_sessions_after"]),
            raw["schedule"]["timezone"],
        ),
        limits=Limits(**raw["limits"]),
        models=Models(**raw["models"]),
        identities=Identities(**raw["identities"]),
        github_app=GitHubAppConfig(
            app_id=int(raw["github_app"]["app_id"]),
            installation_id=int(raw["github_app"]["installation_id"]),
            private_key=_keychain(raw["github_app"]["private_key"]),
        ),
        plane=PlaneConfig(
            url=raw["plane"]["url"].rstrip("/"),
            workspace=raw["plane"]["workspace"],
            api_key=_keychain(raw["plane"]["api_key"]),
            client_cert=_path(raw["plane"].get("client_cert")),
            client_key=_path(raw["plane"].get("client_key")),
        ),
        claude=ClaudeConfig(oauth_token=_keychain(raw["claude"]["oauth_token"])),
        paths=Paths(**{k: _path(v) for k, v in raw["paths"].items()}),
        container=Container(**raw["container"]),
        nudge=Nudge(**raw["nudge"]),
        atc=AtcConfig(url=raw["atc"]["url"].rstrip("/")),
    )


def paused_reader(pause_file: Path) -> Callable[[], bool]:
    """Re-read the pause file, so a pause takes effect before the next session. Paused when
    `pause_file` exists; its contents, if any, are the reason, logged to stderr once per change."""
    logged: list[str | None] = [None]

    def read() -> bool:
        try:
            reason = Path(pause_file).read_text(errors="replace").strip()
        except FileNotFoundError:
            logged[0] = None
            return False
        except OSError:
            reason = "(unreadable)"
        if reason != logged[0]:
            print(f"bau: paused by {pause_file}: {reason or '(no reason given)'}", file=sys.stderr)
            logged[0] = reason
        return True

    return read
