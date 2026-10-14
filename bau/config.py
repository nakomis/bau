from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import yaml


@dataclass(frozen=True)
class KeychainRef:
    service: str
    account: str


@dataclass(frozen=True)
class Limits:
    open_pr_ceiling: int
    pace_margin: float
    five_hour_kill: float
    session_wall_clock_minutes: int
    session_budget_usd: float
    fail_closed_sessions: int
    ci_fix_sessions_per_pr: int
    too_big_files: int
    too_big_lines: int
    sweep_session_wall_clock_minutes: int


@dataclass(frozen=True)
class Schedule:
    no_new_sessions_after: time
    timezone: str
    sweep_no_new_sessions_after: time


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
class ClaudeConfig:
    oauth_token: KeychainRef


@dataclass(frozen=True)
class Paths:
    repos: Path
    work: Path
    logs: Path
    snapshot: Path


@dataclass(frozen=True)
class Container:
    image: str
    network: str
    proxy: str


@dataclass(frozen=True)
class Config:
    paused: bool
    schedule: Schedule
    limits: Limits
    models: Models
    identities: Identities
    github_app: GitHubAppConfig
    plane: PlaneConfig
    claude: ClaudeConfig
    paths: Paths
    container: Container


@dataclass(frozen=True)
class ProjectConfig:
    identifier: str
    repo: str
    deploys: bool
    deploy_action: str = ""


def _keychain(d: dict) -> KeychainRef:
    return KeychainRef(service=d["service"], account=d["account"])


def _path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def _time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    return Config(
        paused=raw["paused"] is True,
        schedule=Schedule(
            _time(raw["schedule"]["no_new_sessions_after"]),
            raw["schedule"]["timezone"],
            _time(raw["schedule"]["sweep_no_new_sessions_after"]),
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
        paths=Paths(**{k: Path(v).expanduser() for k, v in raw["paths"].items()}),
        container=Container(**raw["container"]),
    )


def paused_reader(path: Path, initial: bool) -> Callable[[], bool]:
    """Re-read only the `paused` flag, so a pause takes effect before the next session. True only
    for a literal YAML `true`; a file that will not parse keeps the last value seen."""
    last = [initial]

    def read() -> bool:
        try:
            raw = yaml.safe_load(Path(path).read_text())
        except (OSError, yaml.YAMLError):
            return last[0]
        if not isinstance(raw, dict):
            return last[0]
        last[0] = raw.get("paused") is True
        return last[0]

    return read


def load_projects(path: Path) -> dict[str, ProjectConfig]:
    raw = yaml.safe_load(Path(path).read_text())["projects"]
    return {
        ident: ProjectConfig(ident, p["repo"], bool(p.get("deploys", False)), p.get("deploy_action", ""))
        for ident, p in raw.items()
    }
