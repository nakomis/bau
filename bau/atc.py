"""ATC (nakomis/atc) supplies BAU's project list and the order work is done in.

BAU reads two endpoints: `GET /api/projects?bau_enabled=true` (the projects BAU may work, in board
priority order) and `GET /api/priority` (every open item ref, in priority order). A good read is
cached on Cere; when ATC cannot be reached the cache stands in and the night is recorded as stale.
With neither, the run stops at preflight.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from bau.config import ProjectConfig
from bau.plane import Item

# Plane's priority rank, urgent first: the tie-break within ATC's order, and the whole order for
# items ATC does not list.
PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4, None: 4}


class AtcUnavailable(Exception):
    """ATC could not be read: a transport error, a non-2xx answer, or a body of the wrong shape."""


@dataclass(frozen=True)
class AtcState:
    projects: dict[str, ProjectConfig]  # board order (insertion order)
    priority: dict[str, int]  # ref -> position in ATC's priority order
    stale: bool
    empty: bool = False  # ATC answered with no BAU-enabled projects: the night idles, visibly
    fetched_at: str | None = None  # when ATC was last read successfully (ISO, UTC)


class AtcClient:
    def __init__(
        self,
        url: str,
        cert: Path | None,
        key: Path | None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = url.rstrip("/")
        self._http = httpx.Client(
            cert=(str(cert), str(key)) if cert and key else None,
            timeout=30,
            transport=transport,
        )

    def _get(self, path: str, params: dict | None = None):
        try:
            r = self._http.get(f"{self.url}{path}", params=params)
        except httpx.HTTPError as e:
            raise AtcUnavailable(f"ATC GET {path}: {type(e).__name__}: {e}") from e
        if not r.is_success:
            raise AtcUnavailable(f"ATC GET {path}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise AtcUnavailable(f"ATC GET {path}: not JSON") from e

    def projects(self) -> list[ProjectConfig]:
        rows = self._get("/api/projects", {"bau_enabled": "true"})
        if not isinstance(rows, list):
            raise AtcUnavailable("ATC GET /api/projects: expected a list")
        try:
            return [_project(r) for r in rows if r.get("bau_enabled", True) is not False]
        except (AttributeError, KeyError, TypeError) as e:
            raise AtcUnavailable(f"ATC GET /api/projects: bad row: {e}") from e

    def priority(self) -> list[str]:
        body = self._get("/api/priority")
        refs = body.get("refs") if isinstance(body, dict) else None
        if not isinstance(refs, list):
            raise AtcUnavailable("ATC GET /api/priority: expected {refs: [...]}")
        return [str(r) for r in refs]


def _project(row: dict) -> ProjectConfig:
    return ProjectConfig(
        str(row["identifier"]),
        row.get("repo") or None,
        bool(row.get("deploys")),
        row.get("deploy_action") or "",
    )


def _state(projects: list[ProjectConfig], refs: list[str], stale: bool, fetched_at: str | None) -> AtcState:
    order: dict[str, int] = {}
    for i, ref in enumerate(refs):
        order.setdefault(ref, i)
    return AtcState({p.identifier: p for p in projects}, order, stale, not projects, fetched_at)


def _write_cache(cache: Path, projects: list[ProjectConfig], refs: list[str], fetched_at: str) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    data = {"fetched_at": fetched_at, "projects": [asdict(p) for p in projects], "refs": refs}
    fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=f".{cache.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # on disk before the rename, so a crash never leaves an empty cache
        os.replace(tmp, cache)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_cache(cache: Path) -> AtcState | None:
    try:
        data = json.loads(cache.read_text())
        projects = [ProjectConfig(**p) for p in data["projects"]]
        refs = [str(r) for r in data["refs"]]
        fetched_at = data.get("fetched_at")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return _state(projects, refs, True, fetched_at if isinstance(fetched_at, str) else None)


def load_atc_state(client: AtcClient, cache: Path) -> AtcState:
    """ATC's projects and priority, cached on success. When ATC is unavailable, the cached copy marked
    stale; with no readable cache, AtcUnavailable propagates (the run stops with `preflight: atc`).
    An answer with no projects is used (the night idles, recorded as `empty`) but never replaces a
    cache that has projects, so a later outage still falls back to a useful copy."""
    try:
        projects, refs = client.projects(), client.priority()
    except AtcUnavailable:
        cached = _read_cache(cache)
        if cached is None:
            raise
        return cached
    fetched_at = datetime.now(UTC).isoformat()
    previous = _read_cache(cache) if not projects else None
    if previous is not None and previous.projects:
        print(f"bau: ATC listed no BAU projects; keeping the cache at {cache}", file=sys.stderr)
    else:
        try:
            _write_cache(cache, projects, refs, fetched_at)
        except OSError as e:  # a cache that cannot be written must not stop a night ATC answered
            print(f"bau: could not write the ATC cache {cache}: {e}", file=sys.stderr)
    return _state(projects, refs, False, fetched_at)


def priority_key(item: Item, priority: dict[str, int]) -> tuple:
    """ATC's order first; items it does not list come after, in the old urgent-first, oldest-first order."""
    return (priority.get(item.ref, len(priority)), PRIORITY_RANK.get(item.priority, 4), item.created_at)
