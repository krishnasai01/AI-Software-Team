"""The GitHub calls onboarding relies on, against a mock transport: the real API cannot be reached from tests."""

import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core import github as core_github
from core.github import AppAdmin, GitHub, GitHubPermissionError, UserToken

API = "https://api.example"


class Api:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}

    def reply(self, method: str, path: str, body: object = None, status: int = 200) -> None:
        self.routes[(method, path)] = (status, body if body is not None else {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        status, body = self.routes.get((request.method, request.url.path), (200, {}))
        return httpx.Response(status, json=body)

    def bodies(self, method: str, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.calls if r.method == method and r.url.path == path]


@pytest.fixture
def api(monkeypatch):
    fake = Api()
    real = httpx.AsyncClient
    monkeypatch.setattr(
        core_github.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw)
    )
    return fake


@pytest.fixture
def gh():
    return GitHub("operator", app=UserToken("tok-123", API))


async def test_a_users_own_token_authenticates_the_client(api, gh):
    api.reply("GET", "/repos/acme/shop", {"default_branch": "main"})
    assert (await gh.get_repo("acme/shop"))["default_branch"] == "main"
    assert api.calls[0].headers["Authorization"] == "Bearer tok-123"
    assert str(api.calls[0].url).startswith(API)


async def test_files_are_committed_on_a_new_branch_cut_from_the_base(api, gh):
    api.reply("GET", "/repos/acme/shop/git/ref/heads/main", {"object": {"sha": "base-sha"}})
    api.reply("GET", "/repos/acme/shop/git/commits/base-sha", {"tree": {"sha": "base-tree"}})
    api.reply("POST", "/repos/acme/shop/git/blobs", {"sha": "blob-sha"})
    api.reply("POST", "/repos/acme/shop/git/trees", {"sha": "tree-sha"})
    api.reply("POST", "/repos/acme/shop/git/commits", {"sha": "new-commit"})
    sha = await gh.put_files(
        "acme/shop", "main", "onboarding/x", {"a.md": "A", ".github/CODEOWNERS": "B"}, "Add things"
    )
    assert sha == "new-commit"
    assert [b["content"] for b in api.bodies("POST", "/repos/acme/shop/git/blobs")] == ["A", "B"]
    (tree,) = api.bodies("POST", "/repos/acme/shop/git/trees")
    assert tree["base_tree"] == "base-tree"
    assert [e["path"] for e in tree["tree"]] == ["a.md", ".github/CODEOWNERS"]
    assert all(e["mode"] == "100644" and e["sha"] == "blob-sha" for e in tree["tree"])
    (commit,) = api.bodies("POST", "/repos/acme/shop/git/commits")
    assert commit == {"message": "Add things", "tree": "tree-sha", "parents": ["base-sha"]}
    (ref,) = api.bodies("POST", "/repos/acme/shop/git/refs")
    assert ref == {"ref": "refs/heads/onboarding/x", "sha": "new-commit"}  # a new branch, never a force-push


async def test_rulesets_are_created_and_replaced(api, gh):
    api.reply("POST", "/repos/acme/shop/rulesets", {"id": 42})
    assert await gh.create_ruleset("acme/shop", {"name": "agent-company"}) == 42
    await gh.update_ruleset("acme/shop", 42, {"name": "agent-company", "enforcement": "active"})
    assert api.bodies("PUT", "/repos/acme/shop/rulesets/42") == [{"name": "agent-company", "enforcement": "active"}]


async def test_a_refused_write_raises_with_githubs_own_words(api, gh):
    api.reply("POST", "/repos/acme/shop/rulesets", {"message": "Upgrade to GitHub Pro"}, status=403)
    with pytest.raises(GitHubPermissionError, match="Upgrade to GitHub Pro"):
        await gh.create_ruleset("acme/shop", {})


async def test_issues_can_be_listed_without_a_label(api, gh):
    api.reply("GET", "/repos/acme/shop/issues", [{"number": 1}, {"number": 2, "pull_request": {}}])
    assert [i["number"] for i in await gh.list_issues("acme/shop")] == [1]  # pull requests are not issues
    assert "labels" not in api.calls[0].url.params and api.calls[0].url.params["state"] == "all"
    await gh.list_issues("acme/shop", "agent-ready", "open")
    assert api.calls[1].url.params["labels"] == "agent-ready"
    await gh.list_open_issues("acme/shop", "agent-ready")  # the old entry point still works
    assert api.calls[2].url.params["state"] == "open"


async def test_check_runs_are_found_by_name(api, gh):
    api.reply("GET", "/repos/acme/shop/commits/abc/check-runs", {"check_runs": [{"name": "agent-company/policy"}]})
    assert (await gh.check_run("acme/shop", "abc", "agent-company/policy"))["name"] == "agent-company/policy"
    assert api.calls[0].url.params["check_name"] == "agent-company/policy"
    api.reply("GET", "/repos/acme/shop/commits/abc/check-runs", {"check_runs": []})
    assert await gh.check_run("acme/shop", "abc", "agent-company/policy") is None


def keypair() -> tuple[str, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    return pem, key.public_key()


async def test_an_app_authenticates_as_itself_with_a_signed_jwt(api):
    pem, public = keypair()
    admin = AppAdmin(7, pem, API)
    api.reply("GET", "/app", {"slug": "team-orch", "permissions": {"contents": "write"}, "events": ["issues"]})
    assert (await admin.info())["slug"] == "team-orch"
    token = api.calls[0].headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, public, algorithms=["RS256"])
    assert claims["iss"] == "7" and claims["exp"] - claims["iat"] <= 600  # GitHub allows at most ten minutes


async def test_the_webhook_is_configured_as_json_with_the_secret(api):
    pem, _ = keypair()
    admin = AppAdmin(7, pem, API)
    await admin.set_hook_config("https://hooks.example/webhooks/github", "whsec")
    (body,) = api.bodies("PATCH", "/app/hook/config")
    assert body == {
        "url": "https://hooks.example/webhooks/github",
        "content_type": "json",
        "secret": "whsec",
        "insecure_ssl": "0",
    }
    api.reply("GET", "/app/hook/config", {"url": "https://hooks.example/webhooks/github"})
    assert (await admin.hook_config())["url"].endswith("/webhooks/github")


async def test_installation_is_a_yes_or_no_and_other_errors_are_not_hidden(api):
    pem, _ = keypair()
    admin = AppAdmin(7, pem, API)
    api.reply("GET", "/repos/acme/shop/installation", {"id": 1})
    assert await admin.installed_on("acme/shop") is True
    api.reply("GET", "/repos/acme/shop/installation", {"message": "Not Found"}, status=404)
    assert await admin.installed_on("acme/shop") is False
    api.reply("GET", "/repos/acme/shop/installation", {}, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        await admin.installed_on("acme/shop")
