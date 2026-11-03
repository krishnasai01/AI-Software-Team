"""GitHub Issues, PRs and Projects, acting as one role's GitHub App.

Every role has its own App with least privilege, so a `Tickets("reviewer")` simply cannot push
code: GitHub enforces the roster, not this module.
"""

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from core.settings import get_settings

AGENT_BRANCH_PREFIX = "agent/issue-"

# Labels that carry ticket state (see the plan's "Ticket lifecycle").
AGENT_READY = "agent-ready"
NEEDS_BREAKDOWN = "needs-breakdown"
QA_PASSED = "qa-passed"
NEEDS_HUMAN = "needs-human"


class GitHubPermissionError(Exception):
    """403/404 on a write: the App lacks the permission. Never retried."""


@dataclass(frozen=True)
class TicketRef:
    repo: str  # "owner/name"
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def branch(self) -> str:
        return f"{AGENT_BRANCH_PREFIX}{self.number}"

    @property
    def workflow_id(self) -> str:
        return f"ticket-{self.repo.replace('/', '-')}-{self.number}"


def issue_number_from_branch(branch: str) -> int | None:
    if not branch.startswith(AGENT_BRANCH_PREFIX):
        return None
    tail = branch.removeprefix(AGENT_BRANCH_PREFIX)
    return int(tail) if tail.isdigit() else None


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
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

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


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code in (401, 403, 404) and r.request.method != "GET":
        raise GitHubPermissionError(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
    r.raise_for_status()


class Tickets:
    def __init__(self, role: str, app: GitHubApp | None = None):
        self.app = app or GitHubApp(role)

    async def _client(self, repo: str) -> httpx.AsyncClient:
        token = await self.app.token(repo)
        return httpx.AsyncClient(
            base_url=self.app.api,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )

    async def _request(self, repo: str, method: str, path: str, **kw: Any) -> Any:
        async with await self._client(repo) as c:
            r = await c.request(method, path, **kw)
            _raise_for_status(r)
            return r.json() if r.content else None

    # Issues

    async def get_issue(self, t: TicketRef) -> dict:
        return await self._request(t.repo, "GET", f"/repos/{t.repo}/issues/{t.number}")

    async def list_open_issues(self, repo: str, label: str) -> list[dict]:
        issues = await self._request(
            repo, "GET", f"/repos/{repo}/issues", params={"labels": label, "state": "open", "per_page": 100}
        )
        return [i for i in issues if "pull_request" not in i]

    async def add_labels(self, t: TicketRef, *labels: str) -> None:
        await self._request(t.repo, "POST", f"/repos/{t.repo}/issues/{t.number}/labels", json={"labels": list(labels)})

    async def remove_label(self, t: TicketRef, label: str) -> None:
        async with await self._client(t.repo) as c:
            r = await c.delete(f"/repos/{t.repo}/issues/{t.number}/labels/{label}")
            if r.status_code != 404:  # already absent is fine
                _raise_for_status(r)

    async def comment(self, t: TicketRef, body: str) -> None:
        await self._request(t.repo, "POST", f"/repos/{t.repo}/issues/{t.number}/comments", json={"body": body})

    # Pull requests

    async def find_open_pr(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/", 1)[0]
        prs = await self._request(
            repo, "GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}
        )
        return prs[0] if prs else None

    async def list_open_prs(self, repo: str) -> list[dict]:
        return await self._request(repo, "GET", f"/repos/{repo}/pulls", params={"state": "open", "per_page": 100})

    async def get_pr(self, repo: str, number: int) -> dict:
        return await self._request(repo, "GET", f"/repos/{repo}/pulls/{number}")

    async def open_or_update_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> dict:
        existing = await self.find_open_pr(repo, branch)
        if existing:
            return await self._request(
                repo, "PATCH", f"/repos/{repo}/pulls/{existing['number']}", json={"title": title, "body": body}
            )
        return await self._request(
            repo, "POST", f"/repos/{repo}/pulls", json={"title": title, "body": body, "head": branch, "base": base}
        )

    async def pr_reviews(self, repo: str, number: int) -> list[dict]:
        return await self._request(repo, "GET", f"/repos/{repo}/pulls/{number}/reviews", params={"per_page": 100})

    async def post_review(
        self, repo: str, number: int, commit_sha: str, event: str, body: str, comments: list[dict] | None = None
    ) -> None:
        """event: APPROVE or REQUEST_CHANGES. comments: [{path, line, body}] on the diff."""
        payload: dict[str, Any] = {"commit_id": commit_sha, "event": event, "body": body}
        if comments:
            payload["comments"] = [
                {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]} for c in comments
            ]
        await self._request(repo, "POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload)

    async def checks_state(self, repo: str, sha: str) -> tuple[str, list[dict]]:
        """Return ("pending" | "success" | "failure" | "none", check runs) for a commit."""
        data = await self._request(repo, "GET", f"/repos/{repo}/commits/{sha}/check-runs", params={"per_page": 100})
        runs = data["check_runs"]
        if not runs:
            return "none", []
        if any(r["status"] != "completed" for r in runs):
            return "pending", runs
        ok = {"success", "neutral", "skipped"}
        return ("success" if all(r["conclusion"] in ok for r in runs) else "failure"), runs

    # Projects v2 board (a mirror for humans; labels are the source of truth for triggers)

    async def set_board_status(self, t: TicketRef, project_id: str, status: str) -> None:
        issue = await self.get_issue(t)
        q_fields = """query($p:ID!){ node(id:$p){ ... on ProjectV2 { field(name:"Status"){
            ... on ProjectV2SingleSelectField { id options { id name } } } } } }"""
        field = (await self._graphql(t.repo, q_fields, {"p": project_id}))["node"]["field"]
        option = next((o["id"] for o in field["options"] if o["name"].lower() == status.lower()), None)
        if option is None:
            raise ValueError(f"board has no Status option named {status!r}")
        add = "mutation($p:ID!,$c:ID!){ addProjectV2ItemById(input:{projectId:$p,contentId:$c}){ item { id } } }"
        item = await self._graphql(t.repo, add, {"p": project_id, "c": issue["node_id"]})
        item_id = item["addProjectV2ItemById"]["item"]["id"]
        set_ = """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){ updateProjectV2ItemFieldValue(
            input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){ projectV2Item { id } } }"""
        await self._graphql(t.repo, set_, {"p": project_id, "i": item_id, "f": field["id"], "o": option})

    async def _graphql(self, repo: str, query: str, variables: dict) -> dict:
        data = await self._request(repo, "POST", "/graphql", json={"query": query, "variables": variables})
        if data.get("errors"):
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]
