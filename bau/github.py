from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
import jwt

API = "https://api.github.com"


def make_jwt(app_id: int, private_key_pem: str, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, private_key_pem, algorithm="RS256"
    )


@dataclass(frozen=True)
class Token:
    value: str
    expires_at: str


class GitHubApp:
    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        installation_id: int,
        transport: httpx.BaseTransport | None = None,
    ):
        self.app_id, self.pem, self.installation_id = app_id, private_key_pem, installation_id
        self._http = httpx.Client(
            base_url=API, timeout=30, transport=transport, headers={"Accept": "application/vnd.github+json"}
        )

    def installation_token(self, repo: str) -> Token:
        name = repo.split("/", 1)[1]
        r = self._http.post(
            f"/app/installations/{self.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {make_jwt(self.app_id, self.pem)}"},
            json={"repositories": [name]},
        )
        r.raise_for_status()
        data = r.json()
        return Token(data["token"], data["expires_at"])


@dataclass(frozen=True)
class PR:
    repo: str
    number: int
    node_id: str
    title: str
    url: str
    head_ref: str
    head_sha: str
    draft: bool
    created_at: str
    merged_at: str | None
    body: str
    mergeable_state: str
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class ThreadComment:
    author: str
    body: str
    created_at: str


@dataclass(frozen=True)
class Thread:
    id: str
    resolved: bool
    comments: list[ThreadComment] = field(default_factory=list)


THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){ pullRequest(number:$number){
    reviewThreads(first:100){ nodes{ id isResolved
      comments(first:100){ nodes{ author{login} body createdAt databaseId } } } } } } }
"""


class GitHubClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=API,
            timeout=30,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _pr(self, repo: str, d: dict) -> PR:
        return PR(
            repo,
            d["number"],
            d["node_id"],
            d["title"],
            d["html_url"],
            d["head"]["ref"],
            d["head"]["sha"],
            bool(d.get("draft")),
            d["created_at"],
            d.get("merged_at"),
            d.get("body") or "",
            d.get("mergeable_state", "unknown"),
            d.get("changed_files", 0),
            d.get("additions", 0),
            d.get("deletions", 0),
        )

    def _get(self, path: str, **params):
        r = self._http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _graphql(self, query: str, variables: dict) -> dict:
        r = self._http.post("/graphql", json={"query": query, "variables": variables})
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(str(body["errors"])[:300])
        return body["data"]

    def open_prs(self, repo: str, author: str) -> list[PR]:
        rows = self._get(f"/repos/{repo}/pulls", state="open", per_page=100)
        return [self._pr(repo, d) for d in rows if d["user"]["login"] == author]

    def pr(self, repo: str, number: int) -> PR:
        return self._pr(repo, self._get(f"/repos/{repo}/pulls/{number}"))

    def merged_prs_since(self, repo: str, author: str, since_iso: str) -> list[PR]:
        rows = self._get(
            f"/repos/{repo}/pulls", state="closed", sort="updated", direction="desc", per_page=100
        )
        return [
            self._pr(repo, d)
            for d in rows
            if d["user"]["login"] == author and d.get("merged_at") and d["merged_at"] > since_iso
        ]

    def review_threads(self, repo: str, number: int) -> list[Thread]:
        owner, name = repo.split("/", 1)
        data = self._graphql(THREADS_QUERY, {"owner": owner, "name": name, "number": number})
        out = []
        for t in data["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
            comments = [
                ThreadComment((c.get("author") or {}).get("login", ""), c["body"], c["createdAt"])
                for c in t["comments"]["nodes"]
            ]
            out.append(Thread(t["id"], bool(t["isResolved"]), comments))
        return out

    def ci_status(self, repo: str, sha: str) -> str:
        runs = self._get(f"/repos/{repo}/commits/{sha}/check-runs")["check_runs"]
        if not runs:
            return "none"
        if any(r["status"] != "completed" for r in runs):
            return "pending"
        if any(r["conclusion"] in ("failure", "timed_out", "cancelled", "action_required") for r in runs):
            return "red"
        return "green"

    def check_summaries(self, repo: str, sha: str) -> str:
        runs = self._get(f"/repos/{repo}/commits/{sha}/check-runs")["check_runs"]
        blocks = []
        for r in runs:
            if r.get("conclusion") in ("success", None):
                continue
            output = r.get("output") or {}
            lines = [f"{r['name']}: {output.get('title', '')}"]
            if output.get("summary"):
                lines.append(output["summary"])
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)[:6000]

    def branch_protected(self, repo: str, branch: str = "main") -> bool:
        r = self._http.get(f"/repos/{repo}/branches/{branch}/protection")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        p = r.json()
        guarded = bool(p.get("required_status_checks") or p.get("required_pull_request_reviews"))
        no_force = not (p.get("allow_force_pushes") or {}).get("enabled", False)
        return guarded and no_force

    def mark_ready(self, node_id: str) -> None:
        self._graphql(
            "mutation($id:ID!){ markPullRequestReadyForReview(input:{pullRequestId:$id}){"
            " clientMutationId } }",
            {"id": node_id},
        )

    def comment(self, repo: str, number: int, body: str) -> None:
        self._http.post(f"/repos/{repo}/issues/{number}/comments", json={"body": body}).raise_for_status()

    def close_pr(self, repo: str, number: int) -> None:
        self._http.patch(f"/repos/{repo}/pulls/{number}", json={"state": "closed"}).raise_for_status()

    def reply_in_thread(self, repo: str, number: int, comment_id: int, body: str) -> None:
        self._http.post(
            f"/repos/{repo}/pulls/{number}/comments/{comment_id}/replies", json={"body": body}
        ).raise_for_status()

    def resolve_thread(self, thread_id: str) -> None:
        self._graphql(
            "mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ clientMutationId } }",
            {"id": thread_id},
        )
