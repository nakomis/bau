import json

import httpx
import pytest

import bau.plane as plane
from bau.plane import Comment, Item, PlaneClient, html_to_text

PROJECT = {"id": "p1", "identifier": "HOME", "name": "Home"}
STATES = [{"id": "s-new", "name": "New"}, {"id": "s-prog", "name": "In progress"}]
LABELS = [{"id": "l-bau", "name": "BAU"}, {"id": "l-blocked", "name": "blocked"}]
MEMBERS = [{"id": "u-m", "email": "martin@nakomis.com"}, {"id": "u-b", "email": "bau@nakomis.com"}]
ITEM = {
    "id": "i1",
    "sequence_id": 42,
    "name": "Fix thing",
    "description_html": "<p>Do <b>it</b></p>",
    "state": "s-new",
    "labels": ["l-bau"],
    "priority": "medium",
    "created_at": "2026-10-01T00:00:00Z",
    "assignees": [],
}


def make(handler):
    return PlaneClient("https://plane.test", "nakomis", "key", transport=httpx.MockTransport(handler))


def routes(request: httpx.Request, calls: list):
    calls.append((request.method, request.url.path, request.content))
    path = request.url.path
    base = "/api/v1/workspaces/nakomis"
    table = {
        f"{base}/projects/": [PROJECT],
        f"{base}/projects/p1/states/": STATES,
        f"{base}/projects/p1/labels/": LABELS,
        f"{base}/projects/p1/members/": MEMBERS,
        f"{base}/projects/p1/work-items/": {"results": [ITEM], "next_page_results": False},
        f"{base}/work-items/HOME-42/": ITEM,
        f"{base}/projects/p1/work-items/i1/comments/": [
            {
                "id": "c1",
                "actor": "u-m",
                "created_at": "2026-10-02T00:00:00Z",
                "comment_html": "<p>please</p>",
            }
        ],
    }
    if request.method == "GET":
        return httpx.Response(200, json=table[path])
    if request.method == "POST" and path.endswith("/comments/"):
        return httpx.Response(201, json={"id": "c9"})
    return httpx.Response(200, json=ITEM)


def test_list_items_filters_by_label_and_formats():
    calls = []
    c = make(lambda r: routes(r, calls))
    items = c.list_items("HOME", "BAU")
    assert items == [
        Item(
            "HOME-42", "i1", "p1", "Fix thing", "Do it", "New", ["BAU"], "medium", "2026-10-01T00:00:00Z", []
        )
    ]
    assert c.list_items("HOME", "blocked") == []


def test_comments_resolve_author_email():
    c = make(lambda r: routes(r, []))
    assert c.comments("HOME-42") == [Comment("c1", "martin@nakomis.com", "2026-10-02T00:00:00Z", "please")]


def test_set_state_and_labels_patch_the_item():
    calls = []
    c = make(lambda r: routes(r, calls))
    c.set_state("HOME-42", "In progress")
    c.add_labels("HOME-42", ["blocked"])
    c.remove_labels("HOME-42", ["BAU"])
    patches = [json.loads(body) for m, p, body in calls if m == "PATCH"]
    assert patches[0] == {"state": "s-prog"}
    assert patches[1] == {"labels": ["l-bau", "l-blocked"]}
    assert patches[2] == {"labels": []}


def test_assign_comment_link():
    calls = []
    c = make(lambda r: routes(r, calls))
    c.assign("HOME-42", "bau@nakomis.com")
    assert c.comment("HOME-42", "hello **there**") == "c9"
    c.link("HOME-42", "https://github.com/x/y/pull/1", "PR #1")
    bodies = [(m, p, json.loads(body)) for m, p, body in calls if body]
    assert (
        "PATCH",
        "/api/v1/workspaces/nakomis/projects/p1/work-items/i1/",
        {"assignees": ["u-b"]},
    ) in bodies
    assert any(p.endswith("/links/") and b["url"].endswith("/pull/1") for _, p, b in bodies)
    assert any(
        p.endswith("/comments/") and "<strong>there</strong>" in b["comment_html"] for _, p, b in bodies
    )


def test_project_url():
    c = make(lambda r: routes(r, []))
    assert c.project_url("HOME-42") == "https://plane.test/nakomis/browse/HOME-42/"


def counting(responses: list):
    """Serve the given responses in order and record how many calls were made."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.method)
        return responses[min(len(seen), len(responses)) - 1]

    return handler, seen


def test_get_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(plane.time, "sleep", lambda s: None)
    handler, seen = counting([httpx.Response(503), httpx.Response(503), httpx.Response(200, json=[PROJECT])])
    c = make(handler)
    assert c._request("GET", "/projects/") == [PROJECT]
    assert len(seen) == 3


def test_post_5xx_is_not_retried(monkeypatch):
    monkeypatch.setattr(plane.time, "sleep", lambda s: None)
    handler, seen = counting([httpx.Response(503)])
    with pytest.raises(RuntimeError, match="503"):
        make(handler)._request("POST", "/x/", json={})
    assert len(seen) == 1


def test_post_429_with_retry_after_is_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr(plane.time, "sleep", sleeps.append)
    handler, seen = counting(
        [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(201, json={"id": "c9"})]
    )
    assert make(handler)._request("POST", "/x/", json={}) == {"id": "c9"}
    assert len(seen) == 2
    assert sleeps == [0.0]


def test_gives_up_without_sleeping_after_final_attempt(monkeypatch):
    sleeps = []
    monkeypatch.setattr(plane.time, "sleep", sleeps.append)
    handler, seen = counting([httpx.Response(503)])
    with pytest.raises(RuntimeError, match="gave up"):
        make(handler)._request("GET", "/projects/")
    assert len(seen) == 5
    assert len(sleeps) == 4


def test_transport_errors_are_wrapped(monkeypatch):
    def handler(request: httpx.Request):
        raise httpx.ConnectError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        make(handler)._request("GET", "/projects/")


def test_unknown_project_state_label_and_member_raise_value_error():
    c = make(lambda r: routes(r, []))
    with pytest.raises(ValueError, match="No Plane project NOPE"):
        c.list_items("NOPE", "BAU")
    with pytest.raises(ValueError, match="Unknown state 'Nope'"):
        c.set_state("HOME-42", "Nope")
    with pytest.raises(ValueError, match="Unknown label"):
        c.add_labels("HOME-42", ["nope"])
    with pytest.raises(ValueError, match="No Plane member nobody@nakomis.com"):
        c.assign("HOME-42", "nobody@nakomis.com")


def test_html_lists_and_headings_become_line_breaks():
    assert html_to_text("<ul><li>a</li><li>b</li></ul><h2>T</h2><p>x</p>") == "a\nb\nT\nx"
    assert html_to_text("<p>one</p><p></p><p></p><p></p><p>two</p>") == "one\n\ntwo"
