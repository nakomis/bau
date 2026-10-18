import json

import httpx
import pytest

from bau.atc import AtcClient, AtcState, AtcUnavailable, load_atc_state, priority_key
from bau.config import ProjectConfig
from bau.plane import Item


def meta(ident, repo, deploys=False, action="", enabled=True):
    return {
        "id": f"uuid-{ident.lower()}",
        "identifier": ident,
        "name": f"Project {ident}",
        "column": "next",
        "repo": repo,
        "deploys": deploys,
        "deploy_action": action,
        "bau_enabled": enabled,
    }


P_HOME = meta("HOME", "nakomis/home-infra", True, "Run the playbook")
P_BAU = meta("BAU", "nakomis/bau")


def fake_atc(projects=(), refs=(), status=200, seen=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, text="bad gateway")
        if request.url.path == "/api/projects":
            return httpx.Response(200, json=list(projects))
        if request.url.path == "/api/priority":
            return httpx.Response(200, json={"refs": list(refs)})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def item(ref, priority=None, created="2026-01-01"):
    return Item(ref, ref.lower(), "p", f"Title {ref}", "d", "New", ["BAU"], priority, created)


def test_client_reads_bau_enabled_projects_in_board_order():
    seen = []
    c = AtcClient("https://atc.test/", None, None, transport=fake_atc(projects=[P_HOME, P_BAU], seen=seen))
    assert c.projects() == [
        ProjectConfig("HOME", "nakomis/home-infra", True, "Run the playbook"),
        ProjectConfig("BAU", "nakomis/bau", False, ""),
    ]
    assert str(seen[0].url) == "https://atc.test/api/projects?bau_enabled=true"


def test_client_skips_disabled_projects_and_tolerates_null_fields():
    loose = meta("PROJ", None) | {"deploys": None, "deploy_action": None}
    off = meta("OFF", "nakomis/off", enabled=False)
    c = AtcClient("https://atc.test", None, None, transport=fake_atc(projects=[loose, off]))
    assert c.projects() == [ProjectConfig("PROJ", None, False, "")]


def test_client_reads_priority_refs():
    c = AtcClient("https://atc.test", None, None, transport=fake_atc(refs=["BAU-2", "HOME-1"]))
    assert c.priority() == ["BAU-2", "HOME-1"]


@pytest.mark.parametrize("status", [404, 500, 502, 503])
def test_client_raises_on_non_2xx(status):
    c = AtcClient("https://atc.test", None, None, transport=fake_atc(status=status))
    with pytest.raises(AtcUnavailable):
        c.projects()
    with pytest.raises(AtcUnavailable):
        c.priority()


def test_client_raises_on_transport_error_and_bad_json():
    def boom(request):
        raise httpx.ConnectError("refused")

    c = AtcClient("https://atc.test", None, None, transport=httpx.MockTransport(boom))
    with pytest.raises(AtcUnavailable):
        c.projects()
    junk = AtcClient(
        "https://atc.test", None, None, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<"))
    )
    with pytest.raises(AtcUnavailable):
        junk.priority()
    shape = AtcClient(
        "https://atc.test", None, None, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(AtcUnavailable):
        shape.projects()


def test_load_writes_cache_then_falls_back_stale(tmp_path):
    ok = AtcClient(
        "https://atc.test", None, None, transport=fake_atc(projects=[P_HOME, P_BAU], refs=["BAU-2", "HOME-1"])
    )
    s = load_atc_state(ok, tmp_path / "c.json")
    assert (list(s.projects), s.priority, s.stale) == (["HOME", "BAU"], {"BAU-2": 0, "HOME-1": 1}, False)
    down = AtcClient("https://atc.test", None, None, transport=fake_atc(status=502))
    s2 = load_atc_state(down, tmp_path / "c.json")
    assert s2.stale and list(s2.projects) == ["HOME", "BAU"]
    assert s2.priority == {"BAU-2": 0, "HOME-1": 1}
    assert s2.projects["HOME"] == ProjectConfig("HOME", "nakomis/home-infra", True, "Run the playbook")


def test_cache_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    ok = AtcClient("https://atc.test", None, None, transport=fake_atc(projects=[P_BAU], refs=["BAU-1"]))
    cache = tmp_path / "sub" / "c.json"
    load_atc_state(ok, cache)
    assert json.loads(cache.read_text())["refs"] == ["BAU-1"]
    assert [p.name for p in cache.parent.iterdir()] == ["c.json"]


def test_no_cache_and_no_atc_raises(tmp_path):
    down = AtcClient("https://atc.test", None, None, transport=fake_atc(status=502))
    with pytest.raises(AtcUnavailable):
        load_atc_state(down, tmp_path / "missing.json")


def test_unreadable_cache_and_no_atc_raises(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text("{not json")
    down = AtcClient("https://atc.test", None, None, transport=fake_atc(status=502))
    with pytest.raises(AtcUnavailable):
        load_atc_state(down, cache)


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


def test_atc_state_keeps_board_order():
    s = AtcState({"Z": ProjectConfig("Z", None, False), "A": ProjectConfig("A", None, False)}, {}, False)
    assert list(s.projects) == ["Z", "A"]
