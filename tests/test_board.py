import json
from datetime import datetime

import httpx
import pytest

from bau.board import BoardClient, BoardState, BoardUnavailable, load_board_state, priority_key
from bau.config import ProjectConfig
from bau.purr import Item


def meta(prefix, repo, deploy_method=None, action=None, enabled=True):
    return {
        "id": f"id-{prefix.lower()}",
        "prefix": prefix,
        "name": f"Project {prefix}",
        "metadata": {
            "repo": repo,
            "deployMethod": deploy_method,
            "deployAction": action,
            "bauEnabled": enabled,
        },
    }


P_HOME = meta("HOME", "nakomis/home-infra", "ansible", "Run the playbook")
P_BAU = meta("BAU", "nakomis/bau")
P_OTHER = meta("ZED", "nakomis/zed")


def card(kind, key, pid=None):
    return {"kind": kind, "id": pid or f"id-{key.lower()}", "key": key, "title": key, "tasks": []}


def columns(*cols):
    names = ["in_progress", "up_next", "backlog"]
    return [{"column": n, "points": 0, "cards": list(c)} for n, c in zip(names, cols, strict=False)]


def fake_board(projects=(), refs=(), status=200, seen=None, board=None):
    """Purr's /projects, /board and /priority. By default the board shows HOME then BAU."""

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "INTERNAL", "message": "bad gateway"}})
        if request.url.path == "/api/projects":
            return httpx.Response(200, json={"items": list(projects)})
        if request.url.path == "/api/board":
            cols = board if board is not None else columns([card("project", p["prefix"]) for p in projects])
            return httpx.Response(200, json={"columns": cols})
        if request.url.path == "/api/priority":
            return httpx.Response(200, json={"items": list(refs)})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def item(ref, priority=None, created="2026-01-01"):
    return Item(ref, ref.lower(), "p", f"Title {ref}", "d", "New", ["BAU"], priority, created)


def test_client_reads_bau_enabled_projects_in_board_order_with_the_key():
    seen = []
    c = BoardClient("https://purr.test/", "k", transport=fake_board(projects=[P_HOME, P_BAU], seen=seen))
    assert c.projects() == [
        ProjectConfig("HOME", "nakomis/home-infra", True, "Run the playbook"),
        ProjectConfig("BAU", "nakomis/bau", False, ""),
    ]
    assert str(seen[0].url) == "https://purr.test/api/projects"
    assert {r.headers["x-api-key"] for r in seen} == {"k"}


def test_board_order_follows_the_first_project_card_across_columns():
    board = columns(
        [card("epic", "EPIC-1"), card("project", "ZED")],  # In progress
        [card("project", "BAU"), card("project", "ZED")],  # Up next: ZED already placed
        [card("project", "HOME")],  # Backlog
    )
    c = BoardClient("https://purr.test", "k", transport=fake_board([P_HOME, P_BAU, P_OTHER], board=board))
    assert [p.identifier for p in c.projects()] == ["ZED", "BAU", "HOME"]


def test_enabled_projects_missing_from_the_board_follow_by_prefix_and_cards_match_by_id():
    other = meta("ABC", "nakomis/abc")
    board = columns([card("project", "?", pid="id-bau")])  # matched by id, not key
    c = BoardClient("https://purr.test", "k", transport=fake_board([P_HOME, other, P_BAU], board=board))
    assert [p.identifier for p in c.projects()] == ["BAU", "ABC", "HOME"]


def test_client_skips_disabled_projects_and_tolerates_missing_metadata():
    loose = meta("PROJ", None) | {"metadata": {"bauEnabled": True, "repo": None, "deployAction": None}}
    off = meta("OFF", "nakomis/off", enabled=False)
    bare = {"id": "x", "prefix": "BARE", "name": "Bare"}  # no metadata at all
    c = BoardClient("https://purr.test", "k", transport=fake_board(projects=[loose, off, bare]))
    assert c.projects() == [ProjectConfig("PROJ", None, False, "")]


def test_deploys_is_whether_a_deploy_method_is_set():
    p = meta("DEP", "nakomis/dep", deploy_method="ansible")
    q = meta("NOD", "nakomis/nod", deploy_method="")
    c = BoardClient("https://purr.test", "k", transport=fake_board(projects=[p, q]))
    assert {x.identifier: x.deploys for x in c.projects()} == {"DEP": True, "NOD": False}


def test_client_reads_priority_refs():
    c = BoardClient("https://purr.test", "k", transport=fake_board(refs=["BAU-2", "HOME-1"]))
    assert c.priority() == ["BAU-2", "HOME-1"]


@pytest.mark.parametrize("status", [404, 500, 502, 503])
def test_client_raises_on_non_2xx(status):
    c = BoardClient("https://purr.test", "k", transport=fake_board(status=status))
    with pytest.raises(BoardUnavailable):
        c.projects()
    with pytest.raises(BoardUnavailable):
        c.priority()


def test_client_raises_on_transport_error_and_bad_json():
    def boom(request):
        raise httpx.ConnectError("refused")

    c = BoardClient("https://purr.test", "k", transport=httpx.MockTransport(boom))
    with pytest.raises(BoardUnavailable):
        c.projects()
    junk = BoardClient(
        "https://purr.test", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<"))
    )
    with pytest.raises(BoardUnavailable):
        junk.priority()
    shape = BoardClient(
        "https://purr.test", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(BoardUnavailable):
        shape.projects()
    with pytest.raises(BoardUnavailable):
        shape.priority()


def test_a_board_that_is_not_columns_is_unavailable():
    def handle(request):
        if request.url.path == "/api/projects":
            return httpx.Response(200, json={"items": [P_BAU]})
        return httpx.Response(200, json={"nope": 1})

    c = BoardClient("https://purr.test", "k", transport=httpx.MockTransport(handle))
    with pytest.raises(BoardUnavailable):
        c.projects()


def test_error_bodies_are_not_needed_to_map_a_failure():
    c = BoardClient(
        "https://purr.test",
        "k",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"error": {"code": "UNAUTHORISED"}})
        ),
    )
    with pytest.raises(BoardUnavailable, match="HTTP 401"):
        c.priority()


def test_load_writes_cache_then_falls_back_stale(tmp_path):
    ok = BoardClient(
        "https://purr.test", "k", transport=fake_board(projects=[P_HOME, P_BAU], refs=["BAU-2", "HOME-1"])
    )
    s = load_board_state(ok, tmp_path / "c.json")
    assert (list(s.projects), s.priority, s.stale) == (["HOME", "BAU"], {"BAU-2": 0, "HOME-1": 1}, False)
    down = BoardClient("https://purr.test", "k", transport=fake_board(status=502))
    s2 = load_board_state(down, tmp_path / "c.json")
    assert s2.stale and list(s2.projects) == ["HOME", "BAU"]
    assert s2.priority == {"BAU-2": 0, "HOME-1": 1}
    assert s2.projects["HOME"] == ProjectConfig("HOME", "nakomis/home-infra", True, "Run the playbook")


def test_cache_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    ok = BoardClient("https://purr.test", "k", transport=fake_board(projects=[P_BAU], refs=["BAU-1"]))
    cache = tmp_path / "sub" / "c.json"
    load_board_state(ok, cache)
    assert json.loads(cache.read_text())["refs"] == ["BAU-1"]
    assert [p.name for p in cache.parent.iterdir()] == ["c.json"]


def test_no_cache_and_no_purr_raises(tmp_path):
    down = BoardClient("https://purr.test", "k", transport=fake_board(status=502))
    with pytest.raises(BoardUnavailable):
        load_board_state(down, tmp_path / "missing.json")


def test_unreadable_cache_and_no_purr_raises(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text("{not json")
    down = BoardClient("https://purr.test", "k", transport=fake_board(status=502))
    with pytest.raises(BoardUnavailable):
        load_board_state(down, cache)


def test_items_missing_from_priority_fall_back_to_old_order():
    pr = {"HOME-1": 0}
    keys = sorted(
        [
            item("HOME-3", priority="low", created="2026-01-02"),
            item("HOME-2", priority="high"),
            item("HOME-1"),
        ],
        key=lambda i: priority_key(i, pr),
    )
    assert [i.ref for i in keys] == ["HOME-1", "HOME-2", "HOME-3"]


def test_priority_key_ties_break_by_created():
    keys = sorted(
        [item("A-2", "medium", "2026-02-01"), item("A-1", "medium", "2026-01-01"), item("A-3", "urgent")],
        key=lambda i: priority_key(i, {}),
    )
    assert [i.ref for i in keys] == ["A-3", "A-1", "A-2"]


def test_board_state_keeps_board_order():
    s = BoardState({"Z": ProjectConfig("Z", None, False), "A": ProjectConfig("A", None, False)}, {}, False)
    assert list(s.projects) == ["Z", "A"]


# -- fix round 1: empty answers, fsync, fetched_at -------------------------------
def test_empty_projects_do_not_overwrite_a_non_empty_cache(tmp_path):
    cache = tmp_path / "c.json"
    ok = BoardClient("https://purr.test", "k", transport=fake_board(projects=[P_HOME], refs=["HOME-1"]))
    load_board_state(ok, cache)
    before = cache.read_text()
    empty = BoardClient("https://purr.test", "k", transport=fake_board(projects=[], refs=[]))
    s = load_board_state(empty, cache)
    assert (s.projects, s.empty, s.stale) == ({}, True, False)
    assert cache.read_text() == before


def test_empty_projects_with_no_cache_are_still_cached(tmp_path):
    cache = tmp_path / "c.json"
    empty = BoardClient("https://purr.test", "k", transport=fake_board(projects=[], refs=["X-1"]))
    s = load_board_state(empty, cache)
    assert s.empty and json.loads(cache.read_text())["refs"] == ["X-1"]


def test_non_empty_answer_is_not_empty(tmp_path):
    ok = BoardClient("https://purr.test", "k", transport=fake_board(projects=[P_HOME]))
    assert load_board_state(ok, tmp_path / "c.json").empty is False


def test_cache_records_fetched_at_and_stale_state_carries_it(tmp_path):
    cache = tmp_path / "c.json"
    ok = BoardClient("https://purr.test", "k", transport=fake_board(projects=[P_HOME], refs=[]))
    fresh = load_board_state(ok, cache)
    stamp = json.loads(cache.read_text())["fetched_at"]
    assert datetime.fromisoformat(stamp).tzinfo is not None and fresh.fetched_at == stamp
    down = BoardClient("https://purr.test", "k", transport=fake_board(status=502))
    assert load_board_state(down, cache).fetched_at == stamp


def test_old_cache_without_fetched_at_still_loads(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"projects": [], "refs": []}))
    down = BoardClient("https://purr.test", "k", transport=fake_board(status=502))
    s = load_board_state(down, cache)
    assert s.stale and s.fetched_at is None


def test_cache_is_fsynced_before_replace(tmp_path, monkeypatch):
    import bau.board as board

    events = []
    real_fsync, real_replace = board.os.fsync, board.os.replace
    monkeypatch.setattr(board.os, "fsync", lambda fd: events.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(board.os, "replace", lambda a, b: events.append("replace") or real_replace(a, b))
    ok = BoardClient("https://purr.test", "k", transport=fake_board(projects=[P_HOME]))
    load_board_state(ok, tmp_path / "c.json")
    assert events == ["fsync", "replace"]
