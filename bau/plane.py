from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(?:p|li|h[1-6]|div|tr)>|<br\s*/?>", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n{3,}")
_REF = re.compile(r"^\s*([A-Za-z0-9]+)-(\d+)\s*$")


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
    assignees: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Comment:
    id: str
    author_email: str
    created_at: str
    text: str


def html_to_text(value: str | None) -> str:
    text = _BLOCK_END.sub("\n", value or "")
    text = _BLANK_LINES.sub("\n\n", _TAG.sub("", text))
    return html.unescape(text).strip()


def markdown_to_html(md: str) -> str:
    out = []
    for para in md.strip().split("\n\n"):
        p = html.escape(para)
        p = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", p)
        p = re.sub(r"`(.+?)`", r"<code>\1</code>", p)
        out.append(f"<p>{p.replace(chr(10), '<br>')}</p>")
    return "".join(out)


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


class PlaneClient:
    def __init__(
        self,
        url: str,
        workspace: str,
        api_key: str,
        cert: Path | None = None,
        key: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = url.rstrip("/")
        self.workspace = workspace
        self._root = f"{self.url}/api/v1/workspaces/{workspace}"
        self._http = httpx.Client(
            cert=(str(cert), str(key)) if cert and key else None,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=30,
            transport=transport,
        )
        self._meta: dict[str, dict] = {}

    # ── transport ────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
        """Retry 429 for any method and 5xx for GET only; POSTs and PATCHes are not idempotent."""
        for attempt in range(5):
            try:
                r = self._http.request(method, self._root + path, params=params, json=json)
            except httpx.HTTPError as e:
                raise RuntimeError(f"Plane {method} {path}: {e}") from e
            retriable = r.status_code == 429 or (method == "GET" and r.status_code >= 500)
            if retriable:
                if attempt == 4:
                    raise RuntimeError(f"Plane {method} {path} -> {r.status_code}: gave up after retries")
                time.sleep(_retry_delay(r, attempt))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Plane {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content.strip() else None
        raise RuntimeError(f"Plane {method} {path}: gave up after retries")

    def _get_all(self, path: str) -> list:
        params: dict = {"per_page": 1000}
        out: list = []
        while True:
            page = self._request("GET", path, params=params)
            if isinstance(page, list):
                return page
            out.extend(page.get("results", []))
            if not page.get("next_page_results"):
                return out
            params["cursor"] = page["next_cursor"]

    # ── lookups ──────────────────────────────────────────────────────────
    def _project(self, identifier: str) -> dict:
        if identifier not in self._meta:
            projects = {p["identifier"]: p for p in self._get_all("/projects/")}
            if identifier not in projects:
                raise ValueError(f"No Plane project {identifier}")
            p = projects[identifier]
            pid = p["id"]
            self._meta[identifier] = {
                "project": p,
                "states": {s["name"].lower(): s["id"] for s in self._get_all(f"/projects/{pid}/states/")},
                "state_names": {s["id"]: s["name"] for s in self._get_all(f"/projects/{pid}/states/")},
                "labels": {lbl["name"]: lbl["id"] for lbl in self._get_all(f"/projects/{pid}/labels/")},
                "members": {m["id"]: m.get("email", "") for m in self._get_all(f"/projects/{pid}/members/")},
            }
        return self._meta[identifier]

    def _item(self, ref: str) -> tuple[dict, dict]:
        ident, seq = split_ref(ref)
        return self._project(ident), self._request("GET", f"/work-items/{ident}-{seq}/")

    def _format(self, ident: str, raw: dict) -> Item:
        meta = self._project(ident)
        label_names = {v: k for k, v in meta["labels"].items()}
        return Item(
            ref=f"{ident}-{raw['sequence_id']}",
            id=raw["id"],
            project_id=meta["project"]["id"],
            name=raw["name"],
            description=html_to_text(raw.get("description_html")),
            state=meta["state_names"].get(raw.get("state"), ""),
            labels=sorted(label_names.get(lbl, lbl) for lbl in raw.get("labels", [])),
            priority=raw.get("priority"),
            created_at=raw.get("created_at", ""),
            assignees=[meta["members"].get(a, a) for a in raw.get("assignees", [])],
        )

    # ── reads ────────────────────────────────────────────────────────────
    def list_items(self, identifier: str, label: str) -> list[Item]:
        meta = self._project(identifier)
        rows = self._get_all(f"/projects/{meta['project']['id']}/work-items/")
        items = [self._format(identifier, r) for r in rows]
        return [i for i in items if label in i.labels]

    def get_item(self, ref: str) -> Item:
        meta, raw = self._item(ref)
        return self._format(meta["project"]["identifier"], raw)

    def comments(self, ref: str) -> list[Comment]:
        meta, raw = self._item(ref)
        rows = self._get_all(f"/projects/{meta['project']['id']}/work-items/{raw['id']}/comments/")
        rows.sort(key=lambda c: c.get("created_at") or "")
        return [
            Comment(
                c["id"],
                meta["members"].get(c.get("actor") or c.get("created_by"), ""),
                c.get("created_at", ""),
                html_to_text(c.get("comment_html")),
            )
            for c in rows
        ]

    def project_url(self, ref: str) -> str:
        ident, seq = split_ref(ref)
        return f"{self.url}/{self.workspace}/browse/{ident}-{seq}/"

    # ── writes ───────────────────────────────────────────────────────────
    def _patch(self, ref: str, body: dict) -> None:
        meta, raw = self._item(ref)
        self._request("PATCH", f"/projects/{meta['project']['id']}/work-items/{raw['id']}/", json=body)

    def set_state(self, ref: str, state_name: str) -> None:
        meta, _ = self._item(ref)
        if state_name.lower() not in meta["states"]:
            valid = sorted(set(meta["state_names"].values()))
            raise ValueError(f"Unknown state {state_name!r}; valid: {valid}")
        self._patch(ref, {"state": meta["states"][state_name.lower()]})

    def add_labels(self, ref: str, names: list[str]) -> None:
        meta, raw = self._item(ref)
        unknown = [n for n in names if n not in meta["labels"]]
        if unknown:
            raise ValueError(f"Unknown label(s) {unknown}")
        current = set(raw.get("labels", [])) | {meta["labels"][n] for n in names}
        self._patch(ref, {"labels": sorted(current)})

    def remove_labels(self, ref: str, names: list[str]) -> None:
        meta, raw = self._item(ref)
        current = set(raw.get("labels", [])) - {meta["labels"][n] for n in names if n in meta["labels"]}
        self._patch(ref, {"labels": sorted(current)})

    def assign(self, ref: str, email: str) -> None:
        meta, _ = self._item(ref)
        uid = next((k for k, v in meta["members"].items() if v == email), None)
        if uid is None:
            raise ValueError(f"No Plane member {email}")
        self._patch(ref, {"assignees": [uid]})

    def comment(self, ref: str, markdown: str) -> str:
        meta, raw = self._item(ref)
        c = self._request(
            "POST",
            f"/projects/{meta['project']['id']}/work-items/{raw['id']}/comments/",
            json={"comment_html": markdown_to_html(markdown)},
        )
        return c["id"]

    def link(self, ref: str, url: str, title: str) -> None:
        meta, raw = self._item(ref)
        self._request(
            "POST",
            f"/projects/{meta['project']['id']}/work-items/{raw['id']}/links/",
            json={"url": url, "title": title},
        )
