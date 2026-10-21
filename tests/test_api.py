import json

import pytest
from fastapi.testclient import TestClient

import bau
import bau.version
from bau.api import create_app, static_dir
from bau.config import ProjectConfig
from bau.lock import run_lock
from tests.fakes.clients import FakeGitHub, FakePlane, FakeRepos
from tests.test_abandon import item, picked

PROJECTS = {"HOME": ProjectConfig("HOME", "nakomis/home-infra", True, "")}
TOKEN = "s3cret-token"
AUTH = {"X-Bau-Proxy-Token": TOKEN}


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
    body = r.json()
    assert r.status_code == 200 and body.pop("version") and body == {"schema": 1, "missing": True}


def test_state_served_from_file(cfg, tmp_path):
    cfg.paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.snapshot.write_text(json.dumps({"schema": 1, "open_prs": []}))
    assert client(cfg, tmp_path).get("/state.json").json()["open_prs"] == []


def test_index_served(cfg, tmp_path):
    r = client(cfg, tmp_path).get("/")
    assert r.status_code == 200 and "bau" in r.text


def test_security_headers_on_page_and_api(cfg, tmp_path):
    c = client(cfg, tmp_path)
    for path in ("/", "/state.json"):
        h = c.get(path).headers
        assert "default-src 'self'" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"


def test_index_is_revalidated_but_assets_are_not_forced_to(cfg, tmp_path):
    (tmp_path / "web" / "assets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "web" / "assets" / "app-abc.js").write_text("x")
    c = client(cfg, tmp_path)
    assert c.get("/").headers["cache-control"] == "no-cache"
    assert c.get("/index.html").headers["cache-control"] == "no-cache"
    assert c.get("/assets/app-abc.js").headers.get("cache-control") != "no-cache"


def test_state_stays_uncached(cfg, tmp_path):
    assert client(cfg, tmp_path).get("/state.json").headers["cache-control"] == "no-store"


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
    with run_lock(cfg.paths.lock):
        r = client(cfg, tmp_path, plane).post("/abandon/HOME-42", headers=AUTH)
    assert r.status_code == 409
    assert r.json() == {"error": "another BAU operation is in progress; try again shortly"}


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


def _snap(*refs):
    return {"schema": 1, "open_prs": [{"ref": r} for r in refs], "declined": []}


def test_abandon_drops_the_pr_from_the_snapshot(cfg, tmp_path):
    cfg.paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.snapshot.write_text(json.dumps(_snap("HOME-42", "HOME-43")))
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    c = client(cfg, tmp_path, plane)
    assert c.post("/abandon/HOME-42", headers=AUTH).status_code == 200
    assert [p["ref"] for p in c.get("/state.json").json()["open_prs"]] == ["HOME-43"]


def test_abandon_leaves_a_corrupt_or_missing_snapshot_alone(cfg, tmp_path):
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    c = client(cfg, tmp_path, plane)
    assert c.post("/abandon/HOME-42", headers=AUTH).status_code == 200
    assert not cfg.paths.snapshot.exists()
    cfg.paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.snapshot.write_text("{not json")
    plane = FakePlane({"HOME": [item()]}, comments={"HOME-42": [picked()]})
    c = client(cfg, tmp_path, plane)
    assert c.post("/abandon/HOME-42", headers=AUTH).status_code == 200
    assert cfg.paths.snapshot.read_text() == "{not json"


# --- questions: /answers and /not-bau ---------------------------------------------------------

from bau.markers import Question, questions_comment  # noqa: E402
from bau.plane import Comment  # noqa: E402

_QS = [Question("q1", "Which?", "Option A", "because")]
_ASKED_AT = "2026-10-17T22:00:00Z"
_ANSWERS = {
    "round": 1,
    "asked_at": _ASKED_AT,
    "answers": [{"id": "q1", "accepted": True, "text": "Option A"}],
}


def _waiting(labels=("BAU", "bau-questions")):
    asked = Comment("c1", "bau@nakomis.com", _ASKED_AT, questions_comment(1, "S", _QS))
    return FakePlane({"HOME": [item(labels=labels)]}, comments={"HOME-42": [asked]})


def test_answers_and_not_bau_need_the_token(cfg, tmp_path):
    c = client(cfg, tmp_path, _waiting())
    assert c.post("/answers/HOME-42", json=_ANSWERS).status_code == 403
    assert c.post("/not-bau/HOME-42").status_code == 403
    assert c.post("/answers/HOME-42", json=_ANSWERS, headers={"X-Bau-Proxy-Token": "no"}).status_code == 403


def test_answers_route_posts_and_drops_the_question_row(cfg, tmp_path):
    cfg.paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.snapshot.write_text(
        json.dumps({"schema": 1, "questions": [{"ref": "HOME-42"}, {"ref": "HOME-43"}]})
    )
    plane = _waiting()
    c = client(cfg, tmp_path, plane)
    r = c.post("/answers/HOME-42", json=_ANSWERS, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ref": "HOME-42"}
    assert ("remove_labels", ("HOME-42", ["bau-questions"])) in plane.calls
    assert [q["ref"] for q in c.get("/state.json").json()["questions"]] == ["HOME-43"]


def test_answers_error_statuses(cfg, tmp_path):
    def post(plane, body=_ANSWERS, ref="HOME-42"):
        return client(cfg, tmp_path, plane).post(f"/answers/{ref}", json=body, headers=AUTH)

    assert post(_waiting(), ref="nope").status_code == 400
    assert post(_waiting(), ref="ZZZ-1").json() == {"error": "unknown project"}
    r = post(_waiting(), {**_ANSWERS, "answers": [{"id": "q1", "accepted": False, "text": ""}]})
    assert r.status_code == 400
    assert post(_waiting(), {**_ANSWERS, "round": 2}).status_code == 409
    assert post(_waiting(), {**_ANSWERS, "asked_at": "other"}).status_code == 409
    assert post(_waiting(labels=("BAU",))).status_code == 409
    long = {**_ANSWERS, "answers": [{"id": "q1", "accepted": False, "text": "x" * 2001}]}
    assert post(_waiting(), long).status_code == 422


def test_answers_and_not_bau_during_run_are_409(cfg, tmp_path):
    with run_lock(cfg.paths.lock):
        c = client(cfg, tmp_path, _waiting())
        r1 = c.post("/answers/HOME-42", json=_ANSWERS, headers=AUTH)
        r2 = c.post("/not-bau/HOME-42", headers=AUTH)
    for r in (r1, r2):
        assert r.status_code == 409
        assert r.json() == {"error": "another BAU operation is in progress; try again shortly"}


def test_not_bau_route(cfg, tmp_path):
    plane = _waiting()
    c = client(cfg, tmp_path, plane)
    r = c.post("/not-bau/HOME-42", json={"note": "nah"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ref": "HOME-42"}
    assert ("add_labels", ("HOME-42", ["not-bau"])) in plane.calls
    assert c.post("/not-bau/nope", headers=AUTH).status_code == 400
    assert c.post("/not-bau/ZZZ-1", headers=AUTH).status_code == 400


def test_answers_unexpected_error_is_500_without_detail(cfg, tmp_path):
    class Boom(FakePlane):
        def get_item(self, ref):
            raise RuntimeError("secret-token-abc")

        def add_labels(self, ref, names):
            raise RuntimeError("secret-token-abc")

    c = client(cfg, tmp_path, Boom({}))
    responses = (
        c.post("/answers/HOME-42", json=_ANSWERS, headers=AUTH),
        c.post("/not-bau/HOME-42", headers=AUTH),
    )
    for r in responses:
        assert r.status_code == 500 and r.json() == {"error": "internal error"}
        assert "secret" not in r.text


def test_static_dir_prefers_the_packaged_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(bau, "__file__", str(tmp_path / "bau" / "__init__.py"))
    assert static_dir() is None
    checkout = tmp_path / "web" / "dist"
    checkout.mkdir(parents=True)
    assert static_dir() == checkout
    packaged = tmp_path / "bau" / "web_dist"
    packaged.mkdir(parents=True)
    assert static_dir() == packaged


def test_state_json_carries_the_version(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(bau.version, "VERSION", "0.4.2")
    monkeypatch.setattr(bau.version, "GIT_SHA", "abc1234")
    c = client(cfg, tmp_path)
    expected = {"version": "0.4.2", "sha": "abc1234"}
    assert c.get("/state.json").json()["version"] == expected
    cfg.paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.snapshot.write_text(json.dumps({"schema": 1, "open_prs": []}))
    assert c.get("/state.json").json()["version"] == expected
    assert "version" not in json.loads(cfg.paths.snapshot.read_text())


def test_state_json_marks_an_unstamped_build(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(bau.version, "VERSION", "0.0.0+local")
    monkeypatch.setattr(bau.version, "GIT_SHA", "")
    assert client(cfg, tmp_path).get("/state.json").json()["version"] == {
        "version": "0.0.0+local",
        "sha": "unstamped",
    }
