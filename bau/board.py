"""Purr supplies BAU's project list and the order work is done in.

BAU reads three endpoints: `GET /api/projects` (those whose `metadata.bauEnabled` is set are the
projects BAU may work, with their repo and deploy settings), `GET /api/board` (the order of the
projects: each project's first card across the columns, In progress then Up next then Backlog) and
`GET /api/priority` (every open task ref, in priority order). A good read is cached on Cere; when
Purr cannot be reached the cache stands in and the night is recorded as stale. With neither, the
run stops at preflight.
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
from bau.purr import Item

# Purr's priority rank, urgent first: the tie-break within the priority order, and the whole order
# for items it does not list.
PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4, None: 4}


class BoardUnavailable(Exception):
    """Purr could not be read: a transport error, a non-2xx answer, or a body of the wrong shape."""


@dataclass(frozen=True)
class BoardState:
    projects: dict[str, ProjectConfig]  # board order (insertion order)
    priority: dict[str, int]  # ref -> position in Purr's priority order
    stale: bool
    empty: bool = False  # Purr answered with no BAU-enabled projects: the night idles, visibly
    fetched_at: str | None = None  # when Purr was last read successfully (ISO, UTC)


class BoardClient:
    def __init__(self, url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self._http = httpx.Client(headers={"x-api-key": api_key}, timeout=30, transport=transport)

    def _get(self, path: str, params: dict | None = None):
        try:
            r = self._http.get(f"{self.url}/api{path}", params=params)
        except httpx.HTTPError as e:
            raise BoardUnavailable(f"Purr GET {path}: {type(e).__name__}: {e}") from e
        if not r.is_success:
            raise BoardUnavailable(f"Purr GET {path}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise BoardUnavailable(f"Purr GET {path}: not JSON") from e

    def _items(self, path: str) -> list:
        body = self._get(path)
        rows = body.get("items") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise BoardUnavailable(f"Purr GET {path}: expected {{items: [...]}}")
        return rows

    def projects(self) -> list[ProjectConfig]:
        """The BAU-enabled projects in board order, then any the board does not show, by prefix."""
        try:
            rows = [r for r in self._items("/projects") if (r.get("metadata") or {}).get("bauEnabled")]
            by_key = {str(r["prefix"]): r for r in rows} | {str(r["id"]): r for r in rows if "id" in r}
            ordered: list[str] = []
            for column in self._board_columns():
                for card in column.get("cards", []):
                    if card.get("kind") != "project":
                        continue
                    row = by_key.get(str(card.get("key"))) or by_key.get(str(card.get("id")))
                    if row is not None and row["prefix"] not in ordered:
                        ordered.append(row["prefix"])
            ordered += sorted(r["prefix"] for r in rows if r["prefix"] not in ordered)
            lookup = {r["prefix"]: r for r in rows}
            return [_project(lookup[p]) for p in ordered]
        except (AttributeError, KeyError, TypeError) as e:
            raise BoardUnavailable(f"Purr GET /projects or /board: bad row: {e}") from e

    def _board_columns(self) -> list[dict]:
        body = self._get("/board")
        columns = body.get("columns") if isinstance(body, dict) else None
        if not isinstance(columns, list):
            raise BoardUnavailable("Purr GET /board: expected {columns: [...]}")
        return columns

    def priority(self) -> list[str]:
        return [str(r) for r in self._items("/priority")]


def _project(row: dict) -> ProjectConfig:
    meta = row.get("metadata") or {}
    return ProjectConfig(
        str(row["prefix"]),
        meta.get("repo") or None,
        bool(meta.get("deployMethod")),
        meta.get("deployAction") or "",
    )


def _state(projects: list[ProjectConfig], refs: list[str], stale: bool, fetched_at: str | None) -> BoardState:
    order: dict[str, int] = {}
    for i, ref in enumerate(refs):
        order.setdefault(ref, i)
    return BoardState({p.identifier: p for p in projects}, order, stale, not projects, fetched_at)


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


def _read_cache(cache: Path) -> BoardState | None:
    try:
        data = json.loads(cache.read_text())
        projects = [ProjectConfig(**p) for p in data["projects"]]
        refs = [str(r) for r in data["refs"]]
        fetched_at = data.get("fetched_at")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return _state(projects, refs, True, fetched_at if isinstance(fetched_at, str) else None)


def load_board_state(client: BoardClient, cache: Path) -> BoardState:
    """Purr's projects and priority, cached on success. When Purr is unavailable, the cached copy marked
    stale; with no readable cache, BoardUnavailable propagates (the run stops with `preflight: purr`).
    An answer with no projects is used (the night idles, recorded as `empty`) but never replaces a
    cache that has projects, so a later outage still falls back to a useful copy."""
    try:
        projects, refs = client.projects(), client.priority()
    except BoardUnavailable:
        cached = _read_cache(cache)
        if cached is None:
            raise
        return cached
    fetched_at = datetime.now(UTC).isoformat()
    previous = _read_cache(cache) if not projects else None
    if previous is not None and previous.projects:
        print(f"bau: Purr listed no BAU projects; keeping the cache at {cache}", file=sys.stderr)
    else:
        try:
            _write_cache(cache, projects, refs, fetched_at)
        except OSError as e:  # a cache that cannot be written must not stop a night Purr answered
            print(f"bau: could not write the Purr cache {cache}: {e}", file=sys.stderr)
    return _state(projects, refs, False, fetched_at)


def priority_key(item: Item, priority: dict[str, int]) -> tuple:
    """Purr's order first; items it does not list come after, in the old urgent-first, oldest-first order."""
    return (priority.get(item.ref, len(priority)), PRIORITY_RANK.get(item.priority, 4), item.created_at)
