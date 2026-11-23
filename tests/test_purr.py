import json
from urllib.parse import parse_qs

import httpx
import pytest

import bau.purr as purr
from bau.purr import Comment, Item, PurrClient, PurrError, split_ref

STATES = [
    {"id": "s-new", "name": "New", "group": "unstarted", "order": 0},
    {"id": "s-prog", "name": "In progress", "group": "started", "order": 1},
    {"id": "s-done", "name": "Done", "group": "completed", "order": 2},
]
PROJECT = {"id": "p1", "prefix": "HOME", "name": "Home", "states": STATES, "labels": ["BAU"]}
TASK = {
    "id": "i1",
    "ref": "HOME-42",
    "projectId": "p1",
    "sequence": 42,
    "title": "Fix thing",
    "description": "Do **it**",
    "stateId": "s-new",
    "labels": ["blocked", "BAU"],
    "priority": "medium",
    "assignee": "martin",
    "createdBy": "martin",
    "createdAt": "2026-10-01T00:00:00Z",
    "updatedBy": "bau",
    "updatedAt": "2026-10-03T00:00:00Z",
}
COMMENT = {"id": "c1", "taskRef": "HOME-42", "body": "please", "author": "martin", "createdAt": "2026-10-02"}


def make(handler):
    return PurrClient("https://purr.test/", "key", transport=httpx.MockTransport(handler))


class Purr:
    """A fake Purr that records every request; `pages` lets a test page the task list."""

    def __init__(self, tasks=None, pages=None):
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.tasks = tasks if tasks is not None else [TASK]
        self.pages = pages

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0]  # as sent, so escaping is visible
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, query, body))
        if request.headers["x-api-key"] != "key":
            return httpx.Response(401, json={"error": {"code": "UNAUTHORISED", "message": "no"}})
        if request.method == "GET" and path == "/api/projects/HOME":
            return httpx.Response(200, json=PROJECT)
        if request.method == "GET" and path == "/api/projects/HOME/tasks":
            if self.pages:
                return httpx.Response(200, json=self.pages[query.get("cursor", "")])
            return httpx.Response(200, json={"items": self.tasks})
        if request.method == "GET" and path == "/api/tasks/HOME-42":
            return httpx.Response(200, json=TASK | {"state": STATES[0], "comments": [COMMENT]})
        if request.method == "GET" and path == "/api/tasks/HOME-42/comments":
            return httpx.Response(200, json={"items": [COMMENT, COMMENT | {"id": "c2", "author": "bau"}]})
        if request.method == "POST" and path == "/api/projects/HOME/tasks":
            return httpx.Response(201, json=TASK | {"ref": "HOME-99"})
        if request.method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json=COMMENT | {"id": "c9"})
        if path.startswith("/api/projects/"):
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "no such project"}})
        return httpx.Response(200, json=TASK)


def writes(p: Purr):
    return [(m, path, body) for m, path, _, body in p.calls if m != "GET"]


def test_item_fields_map_from_a_task():
    p = Purr()
    item = make(p).get_item("home-42")
    assert item == Item(
        ref="HOME-42",
        id="i1",
        project_id="p1",
        name="Fix thing",
        description="Do **it**",
        state="New",
        labels=["BAU", "blocked"],
        priority="medium",
        created_at="2026-10-01T00:00:00Z",
        assignees=["martin"],
        updated_at="2026-10-03T00:00:00Z",
        state_group="unstarted",
        updated_by="bau",
    )
    assert p.calls[0][1] == "/api/tasks/HOME-42"  # upper-cased, and the project is not fetched for a detail


def test_every_request_carries_the_api_key_and_a_bad_key_maps_to_an_error():
    p = Purr()
    make(p).list_open_items("HOME")
    assert p.calls  # the fake answers 401 unless x-api-key is "key"
    bad = PurrClient("https://purr.test", "wrong", transport=httpx.MockTransport(Purr()))
    with pytest.raises(PurrError, match="401 UNAUTHORISED: no") as e:
        bad.get_item("HOME-42")
    assert (e.value.status, e.value.code) == (401, "UNAUTHORISED")


def test_unassigned_task_has_no_assignees():
    p = Purr(tasks=[{k: v for k, v in TASK.items() if k != "assignee"}])
    assert make(p).list_open_items("HOME")[0].assignees == []


def test_list_items_asks_for_the_label_and_resolves_state_names():
    p = Purr()
    items = make(p).list_items("HOME", "BAU")
    assert [(i.ref, i.state, i.state_group) for i in items] == [("HOME-42", "New", "unstarted")]
    _, path, query, _ = p.calls[-1]
    assert path == "/api/projects/HOME/tasks" and query["label"] == "BAU"
    assert "all" not in query  # open tasks only


def test_list_open_items_pages_through_the_cursor():
    pages = {
        "": {"items": [TASK], "cursor": "c2"},
        "c2": {"items": [TASK | {"ref": "HOME-43", "id": "i2"}], "cursor": "c3"},
        "c3": {"items": [TASK | {"ref": "HOME-44", "id": "i3"}]},
    }
    p = Purr(pages=pages)
    assert [i.ref for i in make(p).list_open_items("HOME")] == ["HOME-42", "HOME-43", "HOME-44"]
    cursors = [q.get("cursor") for m, path, q, _ in p.calls if path.endswith("/tasks")]
    assert cursors == [None, "c2", "c3"]


def test_comments_are_oldest_first_actors_and_markdown():
    got = make(Purr()).comments("HOME-42")
    assert got == [
        Comment("c1", "martin", "2026-10-02", "please"),
        Comment("c2", "bau", "2026-10-02", "please"),
    ]


def test_comment_posts_markdown_verbatim_and_returns_the_id():
    p = Purr()
    assert make(p).comment("HOME-42", "[bau] done: `x` **y**") == "c9"
    assert writes(p) == [("POST", "/api/tasks/HOME-42/comments", {"body": "[bau] done: `x` **y**"})]


def test_set_state_sends_the_canonical_state_name():
    p = Purr()
    make(p).set_state("HOME-42", "in PROGRESS")
    assert writes(p) == [("POST", "/api/tasks/HOME-42/state", {"state": "In progress"})]


def test_labels_are_added_and_removed_one_at_a_time_and_escaped():
    p = Purr()
    c = make(p)
    c.add_labels("HOME-42", ["not-bau", "needs review"])
    c.remove_labels("HOME-42", ["BAU"])
    assert [(m, path) for m, path, _ in writes(p)] == [
        ("PUT", "/api/tasks/HOME-42/labels/not-bau"),
        ("PUT", "/api/tasks/HOME-42/labels/needs%20review"),
        ("DELETE", "/api/tasks/HOME-42/labels/BAU"),
    ]


def test_assign_and_unassign():
    p = Purr()
    c = make(p)
    c.assign("HOME-42", "bau")
    c.unassign("HOME-42")
    assert writes(p) == [
        ("PUT", "/api/tasks/HOME-42/assignee", {"assignee": "bau"}),
        ("PUT", "/api/tasks/HOME-42/assignee", {"assignee": None}),
    ]


def test_link_and_item_url():
    p = Purr()
    c = make(p)
    c.link("HOME-42", "https://github.com/x/y/pull/1", "PR #1")
    assert writes(p) == [
        ("POST", "/api/tasks/HOME-42/links", {"url": "https://github.com/x/y/pull/1", "title": "PR #1"})
    ]
    assert c.item_url("home-42") == "https://purr.test/tasks/HOME-42"


def test_create_item_default_state_and_named_state():
    p = Purr()
    c = make(p)
    assert c.create_item("HOME", "Title", "Body **md**") == "HOME-99"
    assert c.create_item("home", "T2", "B", state="done") == "HOME-99"
    assert writes(p) == [
        ("POST", "/api/projects/HOME/tasks", {"title": "Title", "description": "Body **md**"}),
        ("POST", "/api/projects/HOME/tasks", {"title": "T2", "description": "B", "state": "Done"}),
    ]


def test_unknown_state_and_assignee_raise_value_error_without_calling_purr():
    p = Purr()
    c = make(p)
    with pytest.raises(ValueError, match="Unknown state 'Nope'"):
        c.set_state("HOME-42", "Nope")
    with pytest.raises(ValueError, match="Unknown state 'Nope'"):
        c.create_item("HOME", "t", "d", state="Nope")
    with pytest.raises(ValueError, match="Cannot assign to 'system'"):
        c.assign("HOME-42", "system")
    assert writes(p) == []


def test_unknown_project_is_a_not_found_error():
    with pytest.raises(PurrError, match="404 NOT_FOUND: no such project") as e:
        make(Purr()).project("NOPE")
    assert e.value.status == 404 and e.value.code == "NOT_FOUND"


def test_error_without_an_error_body_falls_back_to_the_text():
    c = make(lambda r: httpx.Response(400, text="plain failure"))
    with pytest.raises(PurrError, match="400: plain failure"):
        c.get_item("HOME-42")


def test_split_ref():
    assert split_ref(" home-42 ") == ("HOME", 42)
    with pytest.raises(ValueError):
        split_ref("nonsense")


def counting(responses: list):
    """Serve the given responses in order and record how many calls were made."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.method)
        return responses[min(len(seen), len(responses)) - 1]

    return handler, seen


def test_get_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(purr.time, "sleep", lambda s: None)
    handler, seen = counting([httpx.Response(503), httpx.Response(503), httpx.Response(200, json=PROJECT)])
    assert make(handler)._request("GET", "/projects/HOME") == PROJECT
    assert len(seen) == 3


def test_post_5xx_is_not_retried(monkeypatch):
    monkeypatch.setattr(purr.time, "sleep", lambda s: None)
    handler, seen = counting([httpx.Response(503)])
    with pytest.raises(PurrError, match="503"):
        make(handler)._request("POST", "/x", json={})
    assert len(seen) == 1


def test_post_429_with_retry_after_is_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr(purr.time, "sleep", sleeps.append)
    handler, seen = counting(
        [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(201, json={"id": "c9"})]
    )
    assert make(handler)._request("POST", "/x", json={}) == {"id": "c9"}
    assert len(seen) == 2
    assert sleeps == [0.0]


def test_gives_up_without_sleeping_after_final_attempt(monkeypatch):
    sleeps = []
    monkeypatch.setattr(purr.time, "sleep", sleeps.append)
    handler, seen = counting([httpx.Response(503)])
    with pytest.raises(PurrError, match="gave up"):
        make(handler)._request("GET", "/projects/HOME")
    assert len(seen) == 5
    assert len(sleeps) == 4


def test_get_transport_errors_are_retried_then_wrapped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(purr.time, "sleep", sleeps.append)
    calls = []

    def handler(request: httpx.Request):
        calls.append(1)
        raise httpx.ConnectError("boom")

    with pytest.raises(PurrError, match="boom"):
        make(handler)._request("GET", "/projects/HOME")
    assert len(calls) == 5 and len(sleeps) == 4


def test_post_transport_errors_are_wrapped_without_retry(monkeypatch):
    monkeypatch.setattr(purr.time, "sleep", lambda s: None)
    calls = []

    def handler(request: httpx.Request):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(PurrError, match="slow"):
        make(handler)._request("POST", "/tasks/HOME-42/comments", json={"body": "x"})
    assert len(calls) == 1
