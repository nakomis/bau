"""Purr (nakomis/purr), the tracker BAU reads its work from and writes its progress to.

The API contract is `docs/api.md` in the Purr repository. Auth is an `x-api-key` header; the key
maps to one actor, which for BAU is `bau`. Descriptions and comment bodies are Markdown both ways.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

_REF = re.compile(r"^\s*([A-Za-z0-9]+)-(\d+)\s*$")

# Who a task can be assigned to (Purr's `Assignee`); `system` writes but is never assigned.
ASSIGNEES = ("martin", "claude", "bau", "purr3d")

PAGE_LIMIT = 200
ATTEMPTS = 5


@dataclass(frozen=True)
class Item:
    ref: str
    id: str
    project_id: str
    name: str
    description: str
    state: str
    labels: list[str]
    priority: str | None
    created_at: str
    assignees: list[str] = field(default_factory=list)  # actors; Purr has at most one
    updated_at: str = ""
    state_group: str = ""
    updated_by: str = ""  # an actor


@dataclass(frozen=True)
class Comment:
    id: str
    author: str  # an actor: martin, claude, bau, purr3d or system
    created_at: str
    text: str  # Markdown


class PurrError(RuntimeError):
    """A failed Purr call. `status` and `code` are set when Purr answered with `{error: {...}}`."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _retry_delay(r: httpx.Response, attempt: int) -> float:
    retry_after = r.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(30.0, float(retry_after))
        except ValueError:
            pass
    return min(30, 2**attempt)


def split_ref(ref: str) -> tuple[str, int]:
    m = _REF.match(ref or "")
    if not m:
        raise ValueError(f"Expected PROJECT-N, got {ref!r}")
    return m.group(1).upper(), int(m.group(2))


def _norm(ref: str) -> str:
    ident, seq = split_ref(ref)
    return f"{ident}-{seq}"


def _error_text(r: httpx.Response) -> tuple[str | None, str]:
    """(code, message) from Purr's `{error: {code, message}}`, else the start of the body."""
    try:
        err = r.json()["error"]
        return str(err.get("code")), str(err.get("message"))[:300]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, r.text[:300]


class PurrClient:
    def __init__(self, url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self._root = f"{self.url}/api"
        self._http = httpx.Client(
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            timeout=30,
            transport=transport,
        )
        self._meta: dict[str, dict] = {}

    # ── transport ────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
        """Retry 429 for any method, and 5xx and transport errors for GET only: a repeated
        POST could comment twice."""
        for attempt in range(ATTEMPTS):
            try:
                r = self._http.request(method, self._root + path, params=params, json=json)
            except httpx.HTTPError as e:
                if method == "GET" and attempt < ATTEMPTS - 1:
                    time.sleep(min(30, 2**attempt))
                    continue
                raise PurrError(f"Purr {method} {path}: {type(e).__name__}: {e}") from e
            if r.status_code == 429 or (method == "GET" and r.status_code >= 500):
                if attempt == ATTEMPTS - 1:
                    raise PurrError(
                        f"Purr {method} {path} -> {r.status_code}: gave up after retries", r.status_code
                    )
                time.sleep(_retry_delay(r, attempt))
                continue
            if r.status_code >= 400:
                code, message = _error_text(r)
                raise PurrError(
                    f"Purr {method} {path} -> {r.status_code}{f' {code}' if code else ''}: {message}",
                    r.status_code,
                    code,
                )
            return r.json() if r.content.strip() else None
        raise PurrError(f"Purr {method} {path}: gave up after retries")

    def _paged(self, path: str, params: dict | None = None) -> list:
        params = {"limit": PAGE_LIMIT, **(params or {})}
        out: list = []
        while True:
            page = self._request("GET", path, params=params)
            out.extend(page.get("items", []))
            if not page.get("cursor"):
                return out
            params["cursor"] = page["cursor"]

    # ── lookups ──────────────────────────────────────────────────────────
    def project(self, identifier: str) -> dict:
        """The project's states and id (cached). Raises PurrError(404) for an unknown prefix."""
        identifier = identifier.upper()
        if identifier not in self._meta:
            p = self._request("GET", f"/projects/{quote(identifier)}")
            states = sorted(p.get("states", []), key=lambda s: s.get("order", 0))
            self._meta[identifier] = {
                "project": p,
                "states": {s["name"].lower(): s["name"] for s in states},
                "by_id": {s["id"]: s for s in states},
            }
        return self._meta[identifier]

    def _format(self, ident: str, raw: dict) -> Item:
        state = (raw.get("state") if isinstance(raw.get("state"), dict) else None) or self.project(ident)[
            "by_id"
        ].get(raw.get("stateId"), {})
        return Item(
            ref=raw["ref"],
            id=raw["id"],
            project_id=raw.get("projectId", ""),
            name=raw["title"],
            description=raw.get("description") or "",
            state=state.get("name", ""),
            labels=sorted(raw.get("labels", [])),
            priority=raw.get("priority"),
            created_at=raw.get("createdAt", ""),
            assignees=[raw["assignee"]] if raw.get("assignee") else [],
            updated_at=raw.get("updatedAt", "") or "",
            state_group=state.get("group", ""),
            updated_by=raw.get("updatedBy", "") or "",
        )

    # ── reads ────────────────────────────────────────────────────────────
    def _tasks(self, identifier: str, params: dict | None = None) -> list[Item]:
        self.project(identifier)
        rows = self._paged(f"/projects/{quote(identifier.upper())}/tasks", params)
        return [self._format(identifier.upper(), r) for r in rows]

    def list_items(self, identifier: str, label: str) -> list[Item]:
        """Open items carrying `label`. Purr lists only open tasks unless asked otherwise, and every
        caller skips finished items anyway."""
        return self._tasks(identifier, {"label": label})

    def list_open_items(self, identifier: str) -> list[Item]:
        """Every item whose state is not in a completed or cancelled group."""
        return self._tasks(identifier)

    def get_item(self, ref: str) -> Item:
        ident, _ = split_ref(ref)
        return self._format(ident, self._request("GET", f"/tasks/{_norm(ref)}"))

    def comments(self, ref: str) -> list[Comment]:
        rows = self._paged(f"/tasks/{_norm(ref)}/comments")  # oldest first
        return [
            Comment(c["id"], c.get("author", ""), c.get("createdAt", ""), c.get("body") or "") for c in rows
        ]

    def item_url(self, ref: str) -> str:
        return f"{self.url}/tasks/{_norm(ref)}"

    # ── writes ───────────────────────────────────────────────────────────
    def _state_name(self, ident: str, state_name: str) -> str:
        states = self.project(ident)["states"]
        if state_name.lower() not in states:
            raise ValueError(f"Unknown state {state_name!r}; valid: {sorted(states.values())}")
        return states[state_name.lower()]

    def set_state(self, ref: str, state_name: str) -> None:
        name = self._state_name(split_ref(ref)[0], state_name)
        self._request("POST", f"/tasks/{_norm(ref)}/state", json={"state": name})

    def add_labels(self, ref: str, names: list[str]) -> None:
        for n in names:  # Purr adds a label the project lacks, and adding one already present is a no-op
            self._request("PUT", f"/tasks/{_norm(ref)}/labels/{quote(n, safe='')}")

    def remove_labels(self, ref: str, names: list[str]) -> None:
        for n in names:
            self._request("DELETE", f"/tasks/{_norm(ref)}/labels/{quote(n, safe='')}")

    def assign(self, ref: str, actor: str) -> None:
        if actor not in ASSIGNEES:
            raise ValueError(f"Cannot assign to {actor!r}; valid: {list(ASSIGNEES)}")
        self._request("PUT", f"/tasks/{_norm(ref)}/assignee", json={"assignee": actor})

    def unassign(self, ref: str) -> None:
        self._request("PUT", f"/tasks/{_norm(ref)}/assignee", json={"assignee": None})

    def comment(self, ref: str, markdown: str) -> str:
        return self._request("POST", f"/tasks/{_norm(ref)}/comments", json={"body": markdown})["id"]

    def create_item(self, identifier: str, name: str, description_md: str, state: str | None = None) -> str:
        """Create a task and return its ref. With no state, the project's default state is used."""
        body: dict = {"title": name, "description": description_md}
        if state is not None:
            body["state"] = self._state_name(identifier.upper(), state)
        raw = self._request("POST", f"/projects/{quote(identifier.upper())}/tasks", json=body)
        return raw["ref"]

    def link(self, ref: str, url: str, title: str) -> None:
        self._request("POST", f"/tasks/{_norm(ref)}/links", json={"url": url, "title": title})
