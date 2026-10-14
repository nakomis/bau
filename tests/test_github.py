import json

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bau.github import GitHubApp, GitHubClient, Thread, ThreadComment, make_jwt

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUB = KEY.public_key()


def test_jwt_is_signed_rs256_with_short_expiry():
    token = make_jwt(12345, PEM, now=1_000_000)
    claims = jwt.decode(token, PUB, algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "12345"}


def test_installation_token_is_scoped_to_one_repo():
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(201, json={"token": "ghs_abc", "expires_at": "2026-10-15T02:00:00Z"})

    app = GitHubApp(12345, PEM, 67890, transport=httpx.MockTransport(handler))
    tok = app.installation_token("nakomis/home-infra")
    assert tok.value == "ghs_abc"
    assert seen["path"] == "/app/installations/67890/access_tokens"
    assert seen["body"] == {"repositories": ["home-infra"]}
    assert seen["auth"].startswith("Bearer ")


PR_JSON = {
    "number": 7,
    "node_id": "PR_x",
    "title": "fix: thing (HOME-42)",
    "html_url": "https://github.com/nakomis/home-infra/pull/7",
    "head": {"ref": "home-42-thing", "sha": "abc123"},
    "draft": True,
    "created_at": "2026-10-15T01:30:00Z",
    "merged_at": None,
    "body": "Plane: HOME-42",
    "mergeable_state": "clean",
    "user": {"login": "nakomis-bau[bot]"},
    "changed_files": 3,
    "additions": 40,
    "deletions": 5,
}


def rest(request: httpx.Request):
    path, method = request.url.path, request.method
    if path == "/repos/nakomis/home-infra/pulls" and method == "GET":
        return httpx.Response(200, json=[PR_JSON, {**PR_JSON, "number": 8, "user": {"login": "nakomis"}}])
    if path == "/repos/nakomis/home-infra/pulls/7":
        return httpx.Response(200, json=PR_JSON)
    if path == "/repos/nakomis/home-infra/commits/abc123/check-runs":
        return httpx.Response(
            200,
            json={
                "check_runs": [
                    {
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "success",
                        "output": {"title": "Lint ok", "summary": "All good"},
                    },
                    {
                        "name": "tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "output": {"title": "3 tests failed", "summary": "test_x broke"},
                    },
                ]
            },
        )
    if path == "/repos/nakomis/home-infra/branches/main/protection":
        return httpx.Response(
            200,
            json={
                "required_status_checks": {"contexts": ["CI Status"]},
                "allow_force_pushes": {"enabled": False},
            },
        )
    if path == "/repos/nakomis/other/branches/main/protection":
        return httpx.Response(404, json={"message": "Branch not protected"})
    if path == "/graphql":
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "author": {"login": "nakomis"},
                                                    "body": "rename this",
                                                    "createdAt": "2026-10-16T09:00:00Z",
                                                }
                                            ]
                                        },
                                    },
                                    {"id": "T2", "isResolved": True, "comments": {"nodes": []}},
                                ]
                            }
                        }
                    }
                }
            },
        )
    return httpx.Response(200, json={})


def client() -> GitHubClient:
    return GitHubClient("ghs_abc", transport=httpx.MockTransport(rest))


def test_open_prs_filters_by_author():
    prs = client().open_prs("nakomis/home-infra", "nakomis-bau[bot]")
    assert [p.number for p in prs] == [7]
    assert prs[0].head_sha == "abc123" and prs[0].draft is True and prs[0].changed_files == 3


def test_ci_status_red_if_any_failure():
    assert client().ci_status("nakomis/home-infra", "abc123") == "red"


def test_check_summaries_lists_only_failing_runs():
    text = client().check_summaries("nakomis/home-infra", "abc123")
    assert "3 tests failed" in text and "test_x broke" in text
    assert "Lint ok" not in text and "All good" not in text


def test_branch_protected_requires_checks_and_no_force_push():
    assert client().branch_protected("nakomis/home-infra") is True
    assert client().branch_protected("nakomis/other") is False


def test_review_threads_graphql_shape():
    threads = client().review_threads("nakomis/home-infra", 7)
    assert threads == [
        Thread("T1", False, [ThreadComment("nakomis", "rename this", "2026-10-16T09:00:00Z")]),
        Thread("T2", True, []),
    ]
