import json

import httpx

from bau.plane import Comment, Item, PlaneClient

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
