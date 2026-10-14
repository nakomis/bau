import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bau.api import create_app
from bau.config import ProjectConfig, load_config
from bau.lock import run_lock
from tests.fakes.clients import FakeGitHub, FakePlane, FakeRepos
from tests.test_abandon import item, picked
from tests.test_config import MINIMAL

PROJECTS = {"HOME": ProjectConfig("HOME", "nakomis/home-infra", True, "")}
TOKEN = "s3cret-token"
AUTH = {"X-Bau-Proxy-Token": TOKEN}


@pytest.fixture
def cfg(tmp_path: Path):
    p = tmp_path / "bau.yaml"
    p.write_text(MINIMAL.replace("/Users/nakomis/bau", str(tmp_path)))
    return load_config(p)


def client(cfg, tmp_path, plane=None, static="make"):
    if static == "make":
        static = tmp_path / "web"
        static.mkdir(exist_ok=True)
        (static / "index.html").write_text("<h1>bau</h1>")
    app = create_app(
        cfg, PROJECTS, plane or FakePlane({}), lambda r: FakeGitHub(), FakeRepos(tmp_path), static, TOKEN
    )
    return TestClient(app)


def test_state_missing_is_reported_not_500(cfg, tmp_path):
    r = client(cfg, tmp_path).get("/state.json")
    assert r.status_code == 200 and r.json() == {"schema": 1, "missing": True}


def test_state_served_from_file(cfg, tmp_path):
    cfg.paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.snapshot.write_text(json.dumps({"schema": 1, "open_prs": []}))
    assert client(cfg, tmp_path).get("/state.json").json()["open_prs"] == []


def test_index_served(cfg, tmp_path):
    r = client(cfg, tmp_path).get("/")
    assert r.status_code == 200 and "bau" in r.text


def test_missing_static_dir_serves_api_only(cfg, tmp_path):
    c = client(cfg, tmp_path, static=tmp_path / "nope")
    assert c.get("/state.json").status_code == 200
    assert c.get("/").status_code == 404


def test_abandon_without_token_is_forbidden(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    r = client(cfg, tmp_path, plane).post("/abandon/HOME-42")
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert not plane.calls


def test_abandon_with_wrong_token_is_forbidden(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    r = client(cfg, tmp_path, plane).post("/abandon/HOME-42", headers={"X-Bau-Proxy-Token": "nope"})
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert not plane.calls


def test_abandon_route(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    c = client(cfg, tmp_path, plane)
    r = c.post("/abandon/HOME-42", json={"note": "nah"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ref": "HOME-42", "pr": None, "restored_state": "Ready"}
    r2 = c.post("/abandon/HOME-42", headers=AUTH)
    assert r2.status_code == 409 and "error" in r2.json()


def test_abandon_during_run_is_409(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    with run_lock(cfg.paths.logs.parent / "run.lock"):
        r = client(cfg, tmp_path, plane).post("/abandon/HOME-42", headers=AUTH)
    assert r.status_code == 409
    assert r.json() == {"error": "a BAU run is in progress; try again after it finishes"}


def test_bad_token_beats_invalid_body(cfg, tmp_path):
    r = client(cfg, tmp_path).post("/abandon/HOME-42", json={"note": "x" * 600})
    assert r.status_code == 403


def test_overlong_note_is_rejected(cfg, tmp_path):
    r = client(cfg, tmp_path).post("/abandon/HOME-42", json={"note": "x" * 501}, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.parametrize("ref", ["home-42", "HOME-042", "ho_me-1", "HOME-0"])
def test_invalid_ref_is_400(cfg, tmp_path, ref):
    r = client(cfg, tmp_path).post(f"/abandon/{ref}", headers=AUTH)
    assert r.status_code == 400 and r.json() == {"error": "invalid ref"}


def test_unknown_project_is_400(cfg, tmp_path):
    r = client(cfg, tmp_path).post("/abandon/ZZZ-1", headers=AUTH)
    assert r.status_code == 400 and r.json() == {"error": "unknown project"}


def test_unexpected_error_is_500_json_without_detail(cfg, tmp_path):
    class Boom(FakePlane):
        def comments(self, ref):
            raise RuntimeError("secret-token-abc")

    c = client(cfg, tmp_path, Boom({"HOME": [item()]}))
    r = c.post("/abandon/HOME-42", headers=AUTH)
    assert r.status_code == 500 and r.json() == {"error": "internal error"}
    assert "secret" not in r.text
