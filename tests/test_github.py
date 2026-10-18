import json

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bau.github import GitHubApp, GitHubClient, IssueComment, Review, Thread, ThreadComment, make_jwt

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUB = KEY.public_key()


def test_jwt_is_signed_rs256_with_short_expiry():
    token = make_jwt(12345, PEM, now=1_000_000)
    # verify_exp is off because the fixed 1970 timestamp is long expired; the claims are what matter.
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
# The /pulls list endpoint omits these four fields; open_prs must hydrate them via /pulls/{n}.
LIST_PR_JSON = {
    k: v
    for k, v in PR_JSON.items()
    if k not in ("mergeable_state", "changed_files", "additions", "deletions")
}
CLOSED_ROWS = [
    {
        **LIST_PR_JSON,
        "number": 11,
        "merged_at": "2026-10-15T12:00:00Z",
        "merge_commit_sha": "deadbeefcafe",
    },  # included
    {**LIST_PR_JSON, "number": 12, "merged_at": "2026-10-10T12:00:00Z"},  # merged before since
    {**LIST_PR_JSON, "number": 13, "user": {"login": "nakomis"}, "merged_at": "2026-10-15T12:00:00Z"},
    {**LIST_PR_JSON, "number": 14, "merged_at": None},  # closed, not merged
]
THREADS_BODY = {
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
                                        "databaseId": 555,
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
}
CHECK_RUNS = {
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
        {
            "name": "deploy",
            "status": "completed",
            "conclusion": "skipped",
            "output": {"title": None, "summary": None},
        },
        {
            "name": "slow",
            "status": "completed",
            "conclusion": "timed_out",
            "output": {"title": None, "summary": None},
        },
    ]
}
GUARDED = {"required_status_checks": {"contexts": ["CI Status"]}, "allow_force_pushes": {"enabled": False}}

# GET responses keyed by path: (status, body).
ROUTES = {
    "/repos/nakomis/home-infra/commits/abc123/check-runs": (200, CHECK_RUNS),
    "/repos/nakomis/home-infra/issues/7/comments": (
        200,
        [
            {"id": 1, "user": {"login": "nakomis-bau[bot]"}, "body": "[bau:ci-fix] 1/3"},
            {"id": 2, "user": None, "body": None},
            {"id": 3, "user": {"login": "nakomis"}, "body": "hello"},
        ],
    ),
    "/repos/nakomis/home-infra/pulls/7/reviews": (
        200,
        [{"id": 9, "user": {"login": "nakomis"}, "state": "COMMENTED", "body": None}],
    ),
    "/repos/nakomis/home-infra/branches/main/protection": (200, GUARDED),
    "/repos/nakomis/other/branches/main/protection": (404, {"message": "Branch not protected"}),
    "/repos/nakomis/other/rules/branches/main": (404, {"message": "Not Found"}),
    "/repos/nakomis/forced/branches/main/protection": (
        200,
        {"required_status_checks": {"contexts": ["CI"]}, "allow_force_pushes": {"enabled": True}},
    ),
    "/repos/nakomis/forced/rules/branches/main": (404, {"message": "Not Found"}),
    "/repos/nakomis/unguarded/branches/main/protection": (200, {"allow_force_pushes": {"enabled": False}}),
    "/repos/nakomis/unguarded/rules/branches/main": (404, {"message": "Not Found"}),
    "/repos/nakomis/empty-checks/branches/main/protection": (
        200,
        {"required_status_checks": {"contexts": [], "checks": []}, "allow_force_pushes": {"enabled": False}},
    ),
    "/repos/nakomis/empty-checks/rules/branches/main": (404, {"message": "Not Found"}),
    "/repos/nakomis/rules-both/branches/main/protection": (404, {"message": "Branch not protected"}),
    "/repos/nakomis/rules-both/rules/branches/main": (
        200,
        [{"type": "pull_request"}, {"type": "non_fast_forward"}, {"type": "deletion"}],
    ),
    "/repos/nakomis/rules-checks/branches/main/protection": (404, {"message": "Branch not protected"}),
    "/repos/nakomis/rules-checks/rules/branches/main": (
        200,
        [{"type": "required_status_checks", "parameters": {}}],
    ),
}


def rest(request: httpx.Request):
    path, method = request.url.path, request.method
    if path == "/repos/nakomis/home-infra/pulls" and method == "GET":
        if request.url.params.get("state") == "closed":
            return httpx.Response(200, json=CLOSED_ROWS)
        return httpx.Response(
            200, json=[LIST_PR_JSON, {**LIST_PR_JSON, "number": 8, "user": {"login": "nakomis"}}]
        )
    if path == "/repos/nakomis/home-infra/pulls/7":
        return httpx.Response(200, json=PR_JSON)
    if path == "/graphql":
        return httpx.Response(200, json=THREADS_BODY)
    if path in ROUTES:
        status, body = ROUTES[path]
        return httpx.Response(status, json=body)
    return httpx.Response(200, json={})


def client(calls: list | None = None) -> GitHubClient:
    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(request)
        return rest(request)

    return GitHubClient("ghs_abc", transport=httpx.MockTransport(handler))


def sent(request: httpx.Request):
    body = json.loads(request.content) if request.content else None
    return request.method, request.url.path, body


def test_open_prs_filters_by_author_and_hydrates_list_fields():
    prs = client().open_prs("nakomis/home-infra", "nakomis-bau[bot]")
    assert [p.number for p in prs] == [7]
    assert prs[0].head_sha == "abc123" and prs[0].draft is True and prs[0].changed_files == 3


def test_ci_status_red_if_any_failure():
    assert client().ci_status("nakomis/home-infra", "abc123") == "red"


def test_check_runs_request_asks_for_a_full_page():
    calls = []
    client(calls).ci_status("nakomis/home-infra", "abc123")
    assert calls[0].url.params["per_page"] == "100"


def test_check_summaries_lists_only_failing_runs():
    text = client().check_summaries("nakomis/home-infra", "abc123")
    assert text == "tests: 3 tests failed\ntest_x broke\n\nslow: "
    assert "Lint ok" not in text and "All good" not in text
    assert "deploy" not in text and "None" not in text


def test_branch_protection_classic_guard():
    c = client()
    assert c.branch_protected("nakomis/home-infra") is True
    assert c.branch_protected("nakomis/forced") is False  # force pushes allowed
    assert c.branch_protected("nakomis/unguarded") is False  # neither checks nor reviews
    assert c.branch_protected("nakomis/empty-checks") is False  # empty contexts and checks
    assert c.branch_protected("nakomis/other") is False  # no classic protection, no rules


def test_branch_protection_rulesets():
    c = client()
    assert c.branch_protected("nakomis/rules-both") is True  # pull_request and non_fast_forward
    assert c.branch_protected("nakomis/rules-checks") is False  # only required_status_checks


def test_review_threads_graphql_shape():
    seen = {}

    def handler(request: httpx.Request):
        if request.url.path == "/graphql":
            seen["body"] = json.loads(request.content)
        return rest(request)

    c = GitHubClient("ghs_abc", transport=httpx.MockTransport(handler))
    threads = c.review_threads("nakomis/home-infra", 7)
    assert threads == [
        Thread("T1", False, [ThreadComment("nakomis", "rename this", "2026-10-16T09:00:00Z", 555)]),
        Thread("T2", True, []),
    ]
    assert seen["body"]["variables"] == {"owner": "nakomis", "name": "home-infra", "number": 7}
    assert "reviewThreads" in seen["body"]["query"]


def test_merged_prs_since_filters_by_author_and_merge_time():
    prs = client().merged_prs_since("nakomis/home-infra", "nakomis-bau[bot]", "2026-10-14T00:00:00Z")
    assert [p.number for p in prs] == [11]
    assert prs[0].merge_commit_sha == "deadbeefcafe"


def test_write_methods_send_expected_requests():
    calls = []
    c = client(calls)
    c.comment("nakomis/home-infra", 7, "hello")
    c.close_pr("nakomis/home-infra", 7)
    c.mark_ready("PR_x")
    c.reply_in_thread("nakomis/home-infra", 7, 555, "done")
    c.resolve_thread("T1")
    assert sent(calls[0]) == ("POST", "/repos/nakomis/home-infra/issues/7/comments", {"body": "hello"})
    assert sent(calls[1]) == ("PATCH", "/repos/nakomis/home-infra/pulls/7", {"state": "closed"})
    method, path, body = sent(calls[2])
    assert (method, path) == ("POST", "/graphql")
    assert body["variables"] == {"id": "PR_x"} and "markPullRequestReadyForReview" in body["query"]
    assert sent(calls[3]) == (
        "POST",
        "/repos/nakomis/home-infra/pulls/7/comments/555/replies",
        {"body": "done"},
    )
    method, path, body = sent(calls[4])
    assert (method, path) == ("POST", "/graphql")
    assert body["variables"] == {"id": "T1"} and "resolveReviewThread" in body["query"]


def test_issue_comments_returns_bodies():
    assert client().issue_comments("nakomis/home-infra", 7) == ["[bau:ci-fix] 1/3", "", "hello"]


def test_issue_comment_rows_carry_ids_and_authors():
    assert client().issue_comment_rows("nakomis/home-infra", 7) == [
        IssueComment(1, "nakomis-bau[bot]", "[bau:ci-fix] 1/3"),
        IssueComment(2, "", ""),
        IssueComment(3, "nakomis", "hello"),
    ]


def test_reviews_carry_state_and_tolerate_null_body():
    assert client().reviews("nakomis/home-infra", 7) == [Review(9, "nakomis", "COMMENTED", "")]
