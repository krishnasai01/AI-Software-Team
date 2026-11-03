"""GitHub, acting as one role's GitHub App. The only module that talks to the GitHub API.

Every role has its own App with least privilege, so `GitHub("reviewer")` simply cannot push code:
GitHub enforces the roster, not this module. Tokens never enter a sandbox.
"""

import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from core.settings import get_settings

AGENT_BRANCH_PREFIX = "agent/issue-"
POLICY_CHECK = "agent-company/policy"

# Labels that carry ticket state.
AGENT_READY = "agent-ready"
NEEDS_BREAKDOWN = "needs-breakdown"
NEEDS_HUMAN = "needs-human"
EPIC = "epic"
SPLIT = "split"
AGENT_REVIEW = "agent-review"  # a collaborator asks for an agent review of an outside PR
AGENT_REVERT = "agent-revert"


class GitHubPermissionError(Exception):
    """403/404 on a write: the App lacks the permission. Never retried."""


def agent_branch(number: int) -> str:
    return f"{AGENT_BRANCH_PREFIX}{number}"


def issue_number_from_branch(branch: str) -> int | None:
    tail = branch.removeprefix(AGENT_BRANCH_PREFIX)
    if tail == branch or not tail.isdigit():
        return None
    return int(tail)


@dataclass(frozen=True)
class CIState:
    state: str  # pending, success, failure, none
    failed: tuple[str, ...] = ()
    failed_run_ids: tuple[int, ...] = ()  # GitHub Actions runs that can be re-run


class GitHubApp:
    """Mints short-lived (1 hour) installation tokens, optionally scoped to one repo."""

    def __init__(self, role: str):
        s = get_settings()
        if role not in s.github_app_ids:
            raise KeyError(f"no GitHub App configured for role {role!r}")
        self.role = role
        self.app_id = s.github_app_ids[role]
        self.private_key = (s.github_app_key_dir / f"{role}.pem").read_text()
        self.api = s.github_api_url
        self._cache: dict[tuple[str, frozenset], tuple[str, float]] = {}

    def _jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}, self.private_key, "RS256")

    async def token(self, repo: str, permissions: dict[str, str] | None = None) -> str:
        key = (repo, frozenset((permissions or {}).items()))
        cached = self._cache.get(key)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
        headers = {"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"}
        async with httpx.AsyncClient(base_url=self.api, headers=headers, timeout=30) as c:
            r = await c.get(f"/repos/{repo}/installation")
            _raise_for_status(r)
            body: dict[str, Any] = {"repositories": [repo.split("/", 1)[1]]}
            if permissions:
                body["permissions"] = permissions
            r = await c.post(f"/app/installations/{r.json()['id']}/access_tokens", json=body)
            _raise_for_status(r)
        token = r.json()["token"]
        self._cache[key] = (token, time.time() + 3600)
        return token


_apps: dict[str, GitHubApp] = {}


def app_for(role: str) -> GitHubApp:
    if role not in _apps:
        _apps[role] = GitHubApp(role)
    return _apps[role]


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code in (401, 403, 404) and r.request.method != "GET":
        raise GitHubPermissionError(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
    r.raise_for_status()


class GitHub:
    def __init__(self, role: str, app: GitHubApp | None = None):
        self.app = app or app_for(role)

    async def _client(self, repo: str) -> httpx.AsyncClient:
        token = await self.app.token(repo)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        return httpx.AsyncClient(base_url=self.app.api, headers=headers, timeout=30)

    async def _request(self, repo: str, method: str, path: str, **kw: Any) -> Any:
        async with await self._client(repo) as c:
            r = await c.request(method, path, **kw)
            _raise_for_status(r)
            return r.json() if r.content else None

    async def _paged(self, repo: str, path: str, params: dict | None = None, key: str | None = None) -> list:
        out: list = []
        params = {"per_page": 100, **(params or {})}
        async with await self._client(repo) as c:
            url: str | None = path
            while url:
                r = await c.get(url, params=params if url == path else None)
                _raise_for_status(r)
                data = r.json()
                out.extend(data[key] if key else data)
                url = r.links.get("next", {}).get("url")
        return out

    async def _graphql(self, repo: str, query: str, variables: dict) -> dict:
        data = await self._request(repo, "POST", "/graphql", json={"query": query, "variables": variables})
        if data.get("errors"):
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    # Issues

    async def get_issue(self, repo: str, number: int) -> dict:
        return await self._request(repo, "GET", f"/repos/{repo}/issues/{number}")

    async def list_open_issues(self, repo: str, label: str) -> list[dict]:
        issues = await self._paged(repo, f"/repos/{repo}/issues", {"labels": label, "state": "open"})
        return [i for i in issues if "pull_request" not in i]

    async def label_actor(self, repo: str, number: int, label: str) -> str | None:
        """Who most recently applied `label` to the issue."""
        events = await self._paged(repo, f"/repos/{repo}/issues/{number}/events")
        actor = None
        for e in events:
            if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label:
                actor = (e.get("actor") or {}).get("login")
        return actor

    async def issue_comments(self, repo: str, number: int, since: str | None = None) -> list[dict]:
        return await self._paged(repo, f"/repos/{repo}/issues/{number}/comments", {"since": since} if since else None)

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int:
        issue = await self._request(
            repo, "POST", f"/repos/{repo}/issues", json={"title": title, "body": body, "labels": labels}
        )
        return issue["number"]

    async def add_labels(self, repo: str, number: int, *labels: str) -> None:
        await self._request(repo, "POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": list(labels)})

    async def remove_label(self, repo: str, number: int, label: str) -> None:
        async with await self._client(repo) as c:
            r = await c.delete(f"/repos/{repo}/issues/{number}/labels/{label}")
            if r.status_code != 404:  # already absent is fine
                _raise_for_status(r)

    async def comment(self, repo: str, number: int, body: str) -> None:
        await self._request(repo, "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    async def set_issue_state(self, repo: str, number: int, state: str) -> None:
        await self._request(repo, "PATCH", f"/repos/{repo}/issues/{number}", json={"state": state})

    # Pull requests

    async def get_pr(self, repo: str, number: int) -> dict:
        return await self._request(repo, "GET", f"/repos/{repo}/pulls/{number}")

    async def list_prs(self, repo: str, state: str = "open", **params: Any) -> list[dict]:
        return await self._paged(repo, f"/repos/{repo}/pulls", {"state": state, **params})

    async def recently_closed_prs(self, repo: str, limit: int = 50) -> list[dict]:
        return await self._request(
            repo,
            "GET",
            f"/repos/{repo}/pulls",
            params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": limit},
        )

    async def find_open_pr(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/", 1)[0]
        prs = await self._request(
            repo, "GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}
        )
        return prs[0] if prs else None

    async def open_or_update_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> dict:
        existing = await self.find_open_pr(repo, branch)
        if existing:
            return await self._request(
                repo, "PATCH", f"/repos/{repo}/pulls/{existing['number']}", json={"title": title, "body": body}
            )
        return await self._request(
            repo, "POST", f"/repos/{repo}/pulls", json={"title": title, "body": body, "head": branch, "base": base}
        )

    async def pr_files(self, repo: str, number: int) -> list[dict]:
        return await self._paged(repo, f"/repos/{repo}/pulls/{number}/files")

    async def pr_reviews(self, repo: str, number: int) -> list[dict]:
        return await self._paged(repo, f"/repos/{repo}/pulls/{number}/reviews")

    async def post_review(
        self, repo: str, number: int, commit_sha: str, event: str, body: str, comments: list[dict] | None = None
    ) -> None:
        """event: APPROVE or REQUEST_CHANGES. comments: [{path, line, body}] on the diff."""
        payload: dict[str, Any] = {"commit_id": commit_sha, "event": event, "body": body}
        if comments:
            payload["comments"] = [
                {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]} for c in comments
            ]
        try:
            await self._request(repo, "POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 422 or not comments:
                raise
            # Inline comments on lines outside the diff are rejected; fold them into the body.
            folded = "\n".join(f"- `{c['path']}:{c['line']}`: {c['body']}" for c in comments)
            payload = {"commit_id": commit_sha, "event": event, "body": f"{body}\n\n{folded}"}
            await self._request(repo, "POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload)

    async def update_branch(self, repo: str, number: int, expected_head_sha: str) -> None:
        await self._request(
            repo, "PUT", f"/repos/{repo}/pulls/{number}/update-branch", json={"expected_head_sha": expected_head_sha}
        )

    async def enable_auto_merge(self, repo: str, pr_node_id: str, method: str = "SQUASH") -> None:
        q = """mutation($id:ID!,$m:PullRequestMergeMethod!){
            enablePullRequestAutoMerge(input:{pullRequestId:$id, mergeMethod:$m}){ clientMutationId } }"""
        await self._graphql(repo, q, {"id": pr_node_id, "m": method})

    async def revert_pr(self, repo: str, pr_node_id: str, title: str, body: str) -> int:
        q = """mutation($id:ID!,$t:String!,$b:String!){
            revertPullRequest(input:{pullRequestId:$id, title:$t, body:$b}){ revertPullRequest { number } } }"""
        data = await self._graphql(repo, q, {"id": pr_node_id, "t": title, "b": body})
        return data["revertPullRequest"]["revertPullRequest"]["number"]

    # CI and checks

    async def ci_state(self, repo: str, sha: str) -> CIState:
        """CI for a commit: check runs (minus our own policy check) plus legacy commit statuses."""
        runs = await self._paged(repo, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
        runs = [r for r in runs if r["name"] != POLICY_CHECK]
        combined = await self._request(repo, "GET", f"/repos/{repo}/commits/{sha}/status")
        statuses = combined.get("statuses", [])
        if not runs and not statuses:
            return CIState("none")
        ok = {"success", "neutral", "skipped"}
        pending = any(r["status"] != "completed" for r in runs) or any(s["state"] == "pending" for s in statuses)
        failed = [r["name"] for r in runs if r["status"] == "completed" and r["conclusion"] not in ok]
        failed += [s["context"] for s in statuses if s["state"] in ("failure", "error")]
        run_ids: set[int] = set()
        for r in runs:
            if r["status"] == "completed" and r["conclusion"] not in ok:
                url = r.get("details_url") or ""
                if "/actions/runs/" in url:
                    tail = url.split("/actions/runs/", 1)[1].split("/", 1)[0]
                    if tail.isdigit():
                        run_ids.add(int(tail))
        if failed:
            return CIState("failure", tuple(failed), tuple(sorted(run_ids)))
        return CIState("pending" if pending else "success")

    async def commit_parents(self, repo: str, sha: str) -> list[str]:
        commit = await self._request(repo, "GET", f"/repos/{repo}/commits/{sha}")
        return [p["sha"] for p in commit.get("parents", [])]

    async def rerun_failed_jobs(self, repo: str, run_id: int) -> None:
        await self._request(repo, "POST", f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs")

    async def post_check(
        self, repo: str, sha: str, name: str, conclusion: str | None, title: str, summary: str
    ) -> None:
        """conclusion None posts an in-progress check (blocks merging without showing a failure)."""
        body: dict[str, Any] = {"name": name, "head_sha": sha, "output": {"title": title, "summary": summary[:60000]}}
        if conclusion is None:
            body["status"] = "in_progress"
        else:
            body["status"], body["conclusion"] = "completed", conclusion
        await self._request(repo, "POST", f"/repos/{repo}/check-runs", json=body)

    # Repository configuration

    async def branch_rules(self, repo: str, branch: str) -> list[dict]:
        return await self._paged(repo, f"/repos/{repo}/rules/branches/{branch}")

    async def file_at(self, repo: str, path: str, ref: str) -> str | None:
        async with await self._client(repo) as c:
            r = await c.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
            if r.status_code == 404:
                return None
            _raise_for_status(r)
            data = r.json()
        return base64.b64decode(data["content"]).decode() if isinstance(data, dict) and "content" in data else None
