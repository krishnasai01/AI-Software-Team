import json
import re
import shlex
import stat
from pathlib import Path

import httpx
import pytest
import yaml

from core.github import POLICY_CHECK, GitHubPermissionError
from onboarding import packs
from onboarding.console import ScriptedConsole
from onboarding.lib import github, hosts
from onboarding.registry import Context, Env, MissingInput, Registry
from onboarding.secrets import SecretStore
from onboarding.shell import Result
from orchestrator.sweeps import config_problems
from tests.onboarding_fakes import FakeAdmin, FakeRepoGitHub, FakeShells

ROOT = Path(__file__).resolve().parents[1]
K1, K2, K3 = ("age1" + c * 58 for c in "qpz")
SECRETS = {
    "postgres_password": "pg-secret-111111",
    "webhook_secret": "wh-secret-222222",
    "litellm_master_key": "sk-litellm-333333",
    "anthropic_api_key": "sk-ant-444444",
}
VALUES = dict(
    operator_login="ann",
    extra_approvers="bob, cy",
    control_private_ip="10.0.0.1",
    worker_private_ip="10.0.0.2",
    public_hostname="hooks.example.com",
    slack_channel="#team",
    timezone="America/Chicago",
    sandbox_runtime="runsc",
    orchestrator_app_id="1",
    engineer_app_id="2",
    reviewer_app_id="3",
)


class World:
    """The software-team pack, wired to fakes: shells, GitHub, and the Apps' own identities."""

    def __init__(self, root: Path, **values: str):
        directory = packs.directory_of("software-team")
        self.pack = packs.pack_module(directory)
        self.registry = Registry()
        hosts.register(self.registry)
        github.register(self.registry)
        self.pack.register(self.registry)
        self.shells = FakeShells()
        self.gh = FakeRepoGitHub("acme/shop")
        self.admins: dict[int, FakeAdmin] = {}
        self.console = ScriptedConsole()
        self.secrets = SecretStore(None)
        self.root = root
        env = Env(
            self.console,
            self.secrets,
            root,
            shells=self.shells,
            services={"github": self.gh, "app_admin": lambda app_id, key: self.admins[app_id]},
        )
        self.ctx = Context(env, values)

    def probe(self, probe_name, /, **args):
        return self.registry.probes[probe_name](self.ctx, **args)

    def act(self, action_name, /, **args):
        return self.registry.actions[action_name].run(self.ctx, **args)

    def preview(self, action_name, /, **args) -> str:
        return self.registry.actions[action_name].preview(self.ctx, **args)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path, **VALUES)


def pem(tmp_path, body="-----BEGIN RSA PRIVATE KEY-----\nPEM-BODY-xyz\n") -> str:
    path = tmp_path / "app.pem"
    path.write_text(body)
    return str(path)


def http_error(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x"), response=httpx.Response(code))


# Rulesets and repo settings


def test_the_ruleset_we_create_satisfies_the_platforms_own_check(w):
    payload = w.pack.ruleset_payload(7, ["test", "lint"])
    assert config_problems(payload["rules"], 7) == []  # the same judge the reconciler uses every few minutes
    assert config_problems(payload["rules"], 8)  # pinned to a different App: not acceptable
    checks = next(r for r in payload["rules"] if r["type"] == "required_status_checks")["parameters"]
    assert {"context": POLICY_CHECK, "integration_id": 7} in checks["required_status_checks"]
    assert {"context": "test"} in checks["required_status_checks"] and {"context": "lint"} in checks[
        "required_status_checks"
    ]
    assert payload["bypass_actors"] == [] and payload["enforcement"] == "active"
    assert payload["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]


def test_ci_check_names_are_parsed_forgivingly(w):
    assert w.pack.parse_checks("test, lint,,test\nbuild ") == ["test", "lint", "build"]
    assert w.pack.parse_checks("") == [] and w.pack.parse_checks(None) == []


async def test_the_ruleset_action_creates_then_updates_and_the_probe_agrees(w):
    args = dict(repo="acme/shop", app_id="7")
    assert (await w.probe("ruleset_ok", **args)).state == "missing"
    created = await w.act("create_ruleset", ci_checks="test", **args)
    assert created.ok and "created" in created.summary and len(w.gh.rulesets) == 1
    assert (await w.probe("ruleset_ok", **args)).done
    again = await w.act("create_ruleset", ci_checks="test, lint", **args)
    assert again.ok and "updated" in again.summary and len(w.gh.rulesets) == 1  # idempotent: one ruleset
    wrong_app = await w.probe("ruleset_ok", repo="acme/shop", app_id="8")
    assert wrong_app.state == "partial" and "pinned to the orchestrator App" in wrong_app.detail


async def test_a_refused_ruleset_says_why(w):
    async def refuse(repo, payload):
        raise GitHubPermissionError("POST /repos/acme/shop/rulesets: 403 Upgrade to GitHub Pro to enable rulesets")

    w.gh.create_ruleset = refuse
    result = await w.act("create_ruleset", repo="acme/shop", app_id="7", ci_checks="")
    assert not result.ok and "Upgrade to GitHub Pro" in result.summary


def test_the_ruleset_preview_says_plainly_what_it_does(w):
    text = w.preview("create_ruleset", repo="acme/shop", app_id="7", ci_checks="test")
    assert "no bypass list" in text and POLICY_CHECK in text and "test" in text and "force pushes" in text
    assert "no CI checks listed" in w.preview("create_ruleset", repo="acme/shop", app_id="7", ci_checks="")


async def test_repo_settings_are_probed_set_and_safe_to_repeat(w):
    want = {"allow_auto_merge": True, "delete_branch_on_merge": True}
    missing = await w.probe("repo_settings", repo="acme/shop", want=want)
    assert missing.state == "missing" and "allow_auto_merge=True" in missing.detail
    for _ in range(2):
        assert (await w.act("set_repo_settings", repo="acme/shop", settings=want)).ok
    assert (await w.probe("repo_settings", repo="acme/shop", want=want)).done


async def test_github_refusals_become_results_not_crashes(w):
    async def deny(*a, **k):
        raise GitHubPermissionError("PATCH /repos/acme/shop: 403 Must have admin rights")

    w.gh.update_repo = deny
    result = await w.act("set_repo_settings", repo="acme/shop", settings={"allow_auto_merge": True})
    assert not result.ok and "Must have admin rights" in result.summary


async def test_repo_access_needs_an_admin_token(w):
    assert (await w.probe("repo_access", repo="acme/shop")).done
    w.gh.settings["permissions"] = {"admin": False}
    partial = await w.probe("repo_access", repo="acme/shop")
    assert partial.state == "partial" and "not an admin" in partial.detail

    async def gone(repo):
        raise http_error(404)

    w.gh.get_repo = gone
    missing = await w.probe("repo_access", repo="acme/shop")
    assert missing.state == "missing" and "token lacks access" in missing.detail


# The product brain pull request


async def test_the_brain_pull_request_adds_the_template_once(tmp_path):
    w = World(ROOT, **VALUES)
    first = await w.act("open_brain_pr", repo="acme/shop", login="ann")
    assert first.ok and "opened" in first.summary
    ((branch, files),) = w.gh.commits
    assert branch.startswith("onboarding/agent-company-brain-")
    assert ".agent-company/policy.yaml" in files and "docs/brain/architecture.md" in files and "CLAUDE.md" in files
    assert "README.md" not in files  # the template's own README is about the template
    assert "@ann" in files[".github/CODEOWNERS"] and "REPLACE_WITH" not in files[".github/CODEOWNERS"]
    assert len(w.gh.prs) == 1

    second = await w.act("open_brain_pr", repo="acme/shop", login="ann")
    assert second.ok and "already open" in second.summary
    assert len(w.gh.commits) == 1 and len(w.gh.prs) == 1  # safe to repeat


async def test_the_brain_pull_request_leaves_existing_files_alone(tmp_path):
    w = World(ROOT, **VALUES)
    w.gh.files_at_base["CLAUDE.md"] = "mine"
    await w.act("open_brain_pr", repo="acme/shop", login="ann")
    assert "CLAUDE.md" not in w.gh.commits[0][1]
    w2 = World(ROOT, **VALUES)
    for path in w.pack.template_files(ROOT, "ann"):
        w2.gh.files_at_base[path] = "x"
    nothing = await w2.act("open_brain_pr", repo="acme/shop", login="ann")
    assert nothing.ok and "already exists" in nothing.summary and not w2.gh.commits


async def test_the_brain_is_done_when_the_policy_is_on_the_default_branch(w):
    probe = lambda: w.probe("file_on_branch", repo="acme/shop", path=".agent-company/policy.yaml")  # noqa: E731
    assert (await probe()).state == "missing"
    w.gh.files_at_base[".agent-company/policy.yaml"] = "auto_merge: {}"
    assert (await probe()).done


# GitHub Apps and the webhook


async def test_an_app_with_the_wrong_permissions_is_named_precisely(tmp_path, w):
    key = pem(tmp_path)
    args = lambda app_id, role: dict(role=role, app_id=str(app_id), pem=key)  # noqa: E731
    good = {"contents": "write", "pull_requests": "write", "metadata": "read"}
    w.admins[1] = FakeAdmin("eng", good)
    assert (await w.probe("github_app", **args(1, "engineer"))).done

    w.admins[2] = FakeAdmin("eng2", {"contents": "write", "administration": "write", "metadata": "read"})
    wrong = await w.probe("github_app", **args(2, "engineer"))
    assert wrong.state == "partial"
    assert "pull_requests:write" in wrong.detail  # missing
    assert "administration:write" in wrong.detail  # more than its role allows


async def test_a_reviewer_that_could_push_code_is_flagged(tmp_path, w):
    w.admins[3] = FakeAdmin("rev", {"contents": "write", "pull_requests": "write", "metadata": "read"})
    result = await w.probe("github_app", role="reviewer", app_id="3", pem=pem(tmp_path))
    assert result.state == "partial" and "more access than its role allows" in result.detail
    assert "contents:write" in result.detail  # the reviewer must be able to read code and never write it


async def test_the_orchestrator_must_subscribe_to_every_event(tmp_path, w):
    perms = w.pack.ROLES["orchestrator"]["permissions"]
    w.admins[4] = FakeAdmin("orch", perms, events=["issues", "status"])
    partial = await w.probe("github_app", role="orchestrator", app_id="4", pem=pem(tmp_path))
    assert partial.state == "partial" and "is not subscribed to events" in partial.detail
    for event in ("issue_comment", "pull_request", "pull_request_review", "check_suite", "check_run"):
        assert event in partial.detail
    w.admins[4]._info["events"] = list(w.pack.EVENTS)
    assert (await w.probe("github_app", role="orchestrator", app_id="4", pem=pem(tmp_path))).done


async def test_unreadable_keys_and_rejected_apps_are_explained(tmp_path, w):
    nokey = await w.probe("github_app", role="engineer", app_id="1", pem=str(tmp_path / "nope.pem"))
    assert nokey.state == "missing" and "cannot read the private key" in nokey.detail

    class Rejecting:
        async def info(self):
            raise httpx.HTTPError("401")

    w.admins[5] = Rejecting()
    rejected = await w.probe("github_app", role="engineer", app_id="5", pem=pem(tmp_path))
    assert rejected.state == "missing" and "did not accept App 5" in rejected.detail


async def test_missing_installations_come_with_the_link_to_install(tmp_path, w):
    key = pem(tmp_path)
    w.admins.update({1: FakeAdmin("orch", installed=["acme/shop"]), 2: FakeAdmin("eng"), 3: FakeAdmin("rev")})
    apps = [
        {"role": r, "app_id": str(i), "pem": key} for i, r in enumerate(("orchestrator", "engineer", "reviewer"), 1)
    ]
    partial = await w.probe("apps_installed", repo="acme/shop", apps=apps)
    assert partial.state == "partial"
    assert "engineer: https://github.com/apps/eng/installations/new" in partial.detail
    assert "reviewer: https://github.com/apps/rev/installations/new" in partial.detail
    assert "orchestrator:" not in partial.detail
    w.admins[2].installed.add("acme/shop")
    w.admins[3].installed.add("acme/shop")
    assert (await w.probe("apps_installed", repo="acme/shop", apps=apps)).done


async def test_the_webhook_is_probed_and_set_with_the_generated_secret(tmp_path, w):
    url = "https://hooks.example.com/webhooks/github"
    w.admins[1] = FakeAdmin(hook={"url": "https://old.example/x", "content_type": "json"})
    wrong = await w.probe("webhook_config", app_id="1", pem=pem(tmp_path), url=url)
    assert wrong.state == "missing" and f"should be {url}" in wrong.detail
    w.secrets.set("webhook_secret", SECRETS["webhook_secret"])
    assert (await w.act("set_webhook", app_id="1", pem=pem(tmp_path), url=url)).ok
    assert w.admins[1].secret == SECRETS["webhook_secret"]
    assert (await w.probe("webhook_config", app_id="1", pem=pem(tmp_path), url=url)).done
    w.admins[1].hook["content_type"] = "form"
    assert (await w.probe("webhook_config", app_id="1", pem=pem(tmp_path), url=url)).state == "missing"


async def test_the_app_key_goes_to_the_servers_on_stdin_never_in_a_command_line(tmp_path, w):
    body = "-----BEGIN RSA PRIVATE KEY-----\nPEM-BODY-xyz\n"
    result = await w.act(
        "install_app_key", role="engineer", pem=pem(tmp_path, body), hosts=["deploy@c", "deploy@w", "deploy@c"]
    )
    assert result.ok
    calls = w.shells.calls
    assert [c.host for c in calls] == ["deploy@c", "deploy@w"]  # each host once
    for call in calls:
        assert call.stdin == body and "PEM-BODY-xyz" not in " ".join(call.argv)
        script = call.argv[-1]
        assert "chmod 0400 /etc/agent-company/github-apps/engineer.pem" in script and "sudo -n" in script
    w.shells.when("tee", err="sudo: a password is required", code=1)
    failed = await w.act("install_app_key", role="engineer", pem=pem(tmp_path), hosts=["deploy@c"])
    assert not failed.ok and "deploy@c" in failed.summary and "password is required" in failed.summary


async def test_app_key_presence_is_checked_on_every_host(w):
    w.shells.when("test -s", code=1)
    result = await w.probe("app_key_installed", role="engineer", hosts=["deploy@c", "local"])
    assert result.state == "missing" and "deploy@c, this machine" in result.detail


# Encryption keys and secrets


SOPS = (ROOT / ".sops.yaml").read_text()


def test_sops_recipients_are_filled_in_without_losing_anything(w):
    update = w.pack.update_sops_recipients
    new = update(SOPS, [K1, K2])
    assert "REPLACE_WITH" not in new and K1 in new and K2 in new
    assert new.startswith("# Secrets are committed only")  # comments survive
    assert update(new, [K1, K2]) == new  # idempotent
    more = update(new, [K3])
    assert K1 in more and K2 in more and K3 in more  # a key already listed is never dropped
    rule = yaml.safe_load(new)["creation_rules"][0]
    assert set(re.split(r"[,\s]+", rule["age"].strip())) == {K1, K2}  # and it is still valid YAML
    assert rule["path_regex"] == r"infra/secrets/.*\.enc\.env$"
    with pytest.raises(ValueError, match="no `age:`"):
        update("creation_rules: []\n", [K1])


async def test_age_keys_are_created_where_missing_and_listed_in_sops_yaml(tmp_path, w):
    (tmp_path / ".sops.yaml").write_text(SOPS)
    w.shells.when("age-keygen", out=K1 + "\n")
    before = await w.probe("age_keys", hosts=["local"])
    assert before.state == "partial" and "placeholder recipients" in before.detail and "this machine" in before.detail
    result = await w.act("setup_age_keys", hosts=["local", "local"])
    assert result.ok and "1 public keys" in result.summary
    assert K1 in (tmp_path / ".sops.yaml").read_text()
    assert (await w.probe("age_keys", hosts=["local"])).done
    # the key file is only created where there is none, and never printed
    assert 'if [ ! -s "$f" ]' in w.shells.ran("age-keygen -o")[0].argv[-1]


async def test_a_host_without_an_age_key_cannot_be_declared_ready(tmp_path, w):
    (tmp_path / ".sops.yaml").write_text(SOPS)
    w.shells.when("age-keygen", err="age-keygen: command not found", code=127)
    probe = await w.probe("age_keys", hosts=["deploy@w"])
    assert probe.state == "missing" and "no age key on deploy@w" in probe.detail
    failed = await w.act("setup_age_keys", hosts=["deploy@w"])
    assert not failed.ok and "deploy@w" in failed.summary
    assert (tmp_path / ".sops.yaml").read_text() == SOPS  # untouched on failure


def compose_text(path: str) -> str:
    return (ROOT / path).read_text()


@pytest.mark.parametrize(
    "which, compose", [("control", "infra/control/docker-compose.yml"), ("worker", "infra/worker/docker-compose.yml")]
)
def test_the_secrets_we_write_match_what_the_compose_files_need(w, which, compose):
    text = compose_text(compose)
    required = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", text))  # variables with no default
    keys = w.pack.ENV_KEYS[which]
    assert required <= set(keys), f"the {which} stack needs {required - set(keys)}, which onboarding would not write"
    assert all(f"${{{k}" in text for k in keys), "onboarding writes variables the stack never reads"
    example = compose_text(".env.example")
    assert all(re.search(rf"^{k}=", example, re.M) for k in keys), "keys missing from .env.example"


async def test_each_stack_gets_only_what_it_needs(w):
    for name, value in SECRETS.items():
        w.secrets.set(name, value)
    ids = {"orchestrator": 1, "engineer": 2, "reviewer": 3}
    control = await w.pack.build_env("control", w.ctx, ids, "team-orch[bot]")
    worker = await w.pack.build_env("worker", w.ctx, ids, "team-orch[bot]")
    assert list(control) == w.pack.ENV_KEYS["control"] and list(worker) == w.pack.ENV_KEYS["worker"]
    assert "ANTHROPIC_API_KEY" not in control and worker["ANTHROPIC_API_KEY"] == SECRETS["anthropic_api_key"]
    assert control["PRIVATE_IP"] == "10.0.0.1" and worker["PRIVATE_IP"] == "10.0.0.2"
    assert control["POSTGRES_PASSWORD"] == worker["POSTGRES_PASSWORD"]
    assert json.loads(control["TRUSTED_LABELERS"]) == ["ann", "team-orch[bot]"]  # the bot keeps split tickets trusted
    assert json.loads(control["HUMAN_APPROVERS"]) == ["ann", "bob", "cy"]
    assert json.loads(worker["GITHUB_APP_IDS"]) == ids


async def test_secrets_that_cannot_be_written_safely_are_refused(w):
    for name, value in SECRETS.items():
        w.secrets.set(name, value)
    ids = {"orchestrator": 1, "engineer": 2, "reviewer": 3}
    w.secrets.set("postgres_password", "two\nlines")
    with pytest.raises(ValueError, match="single line"):
        await w.pack.build_env("control", w.ctx, ids, "b[bot]")
    w.secrets.set("postgres_password", SECRETS["postgres_password"])
    del w.ctx.values["control_private_ip"]  # type: ignore[attr-defined]
    with pytest.raises(MissingInput, match="control_private_ip"):
        await w.pack.build_env("control", w.ctx, ids, "b[bot]")


def secrets_world(tmp_path) -> World:
    w = World(tmp_path, **VALUES, orchestrator_pem=pem(tmp_path))
    for name, value in SECRETS.items():
        w.secrets.set(name, value)
    (tmp_path / ".sops.yaml").write_text(SOPS)
    w.admins[1] = FakeAdmin("team-orch")
    w.shells.reply("sops --config", lambda argv, stdin: Result(0, f"ENC[{len(stdin)}]\n"))
    return w


async def test_secrets_are_encrypted_before_they_touch_disk_or_the_network(tmp_path):
    w = secrets_world(tmp_path)
    result = await w.act(
        "write_secrets", control_host="deploy@c", worker_host="deploy@w", deploy_dir="/opt/agent-company"
    )
    assert result.ok

    sops_calls = w.shells.ran("sops --config")
    assert [c.host for c in sops_calls] == ["local", "local"]
    for call in sops_calls:  # plaintext goes to sops on stdin, and nowhere else
        assert "POSTGRES_PASSWORD=pg-secret-111111" in call.stdin
        assert "/dev/stdin" in call.argv and "--filename-override" in call.argv
        assert not any(v in " ".join(call.argv) for v in SECRETS.values())
    for name in ("control", "worker"):
        path = tmp_path / "infra" / "secrets" / f"{name}.enc.env"
        assert path.read_text().startswith("ENC[") and stat.S_IMODE(path.stat().st_mode) == 0o600
    copies = w.shells.ran("cat >")
    assert {c.host for c in copies} == {"deploy@c", "deploy@w"}
    assert all(c.stdin.startswith("ENC[") for c in copies)  # ciphertext on the wire, not plaintext
    for call in w.shells.calls:
        assert not any(v in " ".join(call.argv) for v in SECRETS.values())
        if call.host != "local":
            assert not any(v in (call.stdin or "") for v in SECRETS.values())


async def test_secrets_for_a_local_stack_are_not_copied_onto_themselves(tmp_path):
    w = secrets_world(tmp_path)
    result = await w.act("write_secrets", control_host="local", worker_host="deploy@w", deploy_dir=str(tmp_path))
    assert result.ok
    assert [c.host for c in w.shells.ran("cat >")] == ["deploy@w"]


async def test_nothing_is_written_if_sops_fails(tmp_path):
    w = secrets_world(tmp_path)
    w.shells.rules.insert(0, ("sops --config", Result(1, "", "no matching creation rules found")))
    result = await w.act("write_secrets", control_host="local", worker_host="local", deploy_dir=str(tmp_path))
    assert not result.ok and "no matching creation rules" in result.summary
    assert not (tmp_path / "infra").exists()


def test_the_secrets_preview_names_variables_never_values(tmp_path):
    w = secrets_world(tmp_path)
    text = w.preview("write_secrets", control_host="deploy@c", worker_host="deploy@w", deploy_dir="/opt/agent-company")
    assert "ANTHROPIC_API_KEY" in text and "GITHUB_WEBHOOK_SECRET" in text
    assert not any(v in text for v in SECRETS.values())


async def test_sops_secrets_are_checked_by_variable_name_only(tmp_path, w):
    (tmp_path / "infra" / "secrets").mkdir(parents=True)
    assert (await w.probe("sops_keys", name="control")).state == "missing"  # no file yet
    (tmp_path / "infra" / "secrets" / "control.enc.env").write_text("ENC[...]")
    everything = {k: "v" for k in w.pack.ENV_KEYS["control"]}
    w.shells.when("sops --decrypt", out=json.dumps(everything))
    assert (await w.probe("sops_keys", name="control")).done
    del everything["TIMEZONE"]
    w.shells.rules.clear()
    w.shells.when("sops --decrypt", out=json.dumps(everything))
    partial = await w.probe("sops_keys", name="control")
    assert partial.state == "partial" and "lacks: TIMEZONE" in partial.detail
    w.shells.rules.clear()
    w.shells.when("sops --decrypt", err="no key could decrypt the data key", code=128)
    assert "cannot decrypt" in (await w.probe("sops_keys", name="control")).detail


# The stacks, the database and products


async def test_the_stacks_start_control_first_and_stop_at_the_first_failure(w):
    ok = await w.act("compose_up", control_host="deploy@c", worker_host="deploy@w", dir="/opt/agent-company")
    assert ok.ok
    first, second = (c.argv[-1] for c in w.shells.calls)
    assert "infra/control/docker-compose.yml" in first and "infra/worker/docker-compose.yml" in second
    assert (
        "cd /opt/agent-company && sops exec-env infra/secrets/control.enc.env "
        "'docker compose -f infra/control/docker-compose.yml up -d --build'"
    ) == first

    w.shells.calls.clear()
    w.shells.when("infra/control", err="port 5432 is already allocated", code=1)
    failed = await w.act("compose_up", control_host="deploy@c", worker_host="deploy@w", dir="/opt/agent-company")
    assert not failed.ok and "control stack did not start on deploy@c" in failed.summary
    assert len(w.shells.calls) == 1  # the worker stack was not even tried


async def test_migrations_are_counted_against_the_files_in_the_repo():
    w = World(ROOT)
    expected = len(list((ROOT / "db" / "migrations").glob("*.sql")))
    w.shells.when("docker exec", out=f"{expected}\n")
    assert (await w.probe("db_migrated", host="deploy@c")).done
    w.shells.rules.clear()
    w.shells.when("docker exec", out="1\n")
    behind = await w.probe("db_migrated", host="deploy@c")
    assert behind.state == "partial" and f"1 of {expected}" in behind.detail
    w.shells.rules.clear()
    w.shells.when("docker exec", code=3)
    assert (await w.probe("db_migrated", host="deploy@c")).state == "missing"


async def test_product_registration_is_probed_and_run_through_the_control_stack(w):
    args = dict(host="deploy@c", name="shop", repo="acme/shop", image="agent-company/sandbox-python:latest")
    assert (await w.probe("product_registered", **args)).state == "missing"
    w.shells.when("docker exec", out="acme/shop agent-company/sandbox-python:latest\n")
    assert (await w.probe("product_registered", **args)).done
    w.shells.rules.clear()
    w.shells.when("docker exec", out="acme/other agent-company/sandbox-node:latest\n")
    assert (await w.probe("product_registered", **args)).state == "partial"
    assert (await w.probe("product_registered", **{**args, "name": "Bad Name"})).state == "missing"


async def test_registration_arguments_stay_single_arguments_whatever_they_contain(w):
    nasty = dict(name="x; touch /tmp/pwn", repo="acme/shop", image="img:latest")
    result = await w.act("add_product", host="deploy@c", dir="/opt/agent-company", **nasty)
    assert result.ok
    script = w.shells.calls[-1].argv[-1]
    outer = shlex.split(script)
    assert outer[:3] == ["cd", "/opt/agent-company", "&&"] and outer[3:5] == ["sops", "exec-env"]
    inner = shlex.split(outer[-1])
    assert inner[-3:] == ["x; touch /tmp/pwn", "acme/shop", "img:latest"]  # each stays one argument
    assert "add-product" in inner and "orchestrator.admin" in inner


def test_registration_preview_says_it_starts_in_shadow_mode(w):
    text = w.preview("add_product", host="deploy@c", dir="/d", name="shop", repo="acme/shop", image="i")
    assert "level 0" in text and "shadow" in text


# The rehearsal


async def test_the_rehearsal_is_followed_stage_by_stage(w):
    probe = lambda: w.probe("rehearsal", repo="acme/shop")  # noqa: E731
    assert (await probe()).detail == "no rehearsal issue yet"

    created = await w.act("create_rehearsal_issue", repo="acme/shop")
    assert created.ok and "created issue #1" in created.summary
    assert w.gh.labels_of(1) == {"agent-ready"}
    again = await w.act("create_rehearsal_issue", repo="acme/shop")
    assert "already exists" in again.summary and len(w.gh.issues) == 1  # safe to repeat

    waiting = await probe()
    assert waiting.state == "partial" and "waiting for the engineer" in waiting.detail

    pr = w.gh.add_pr("agent/issue-1", "sha1")
    opened = await probe()
    assert "PR #2 opened by the engineer" in opened.detail
    assert "waiting for the reviewer" in opened.detail and "no policy check yet" in opened.detail

    w.gh.reviews[pr] = [{"state": "APPROVED", "user": {"login": "ann"}}]  # a person's approval is not the reviewer's
    assert "waiting for the reviewer" in (await probe()).detail

    w.gh.reviews[pr] = [{"state": "APPROVED", "user": {"login": "team-reviewer[bot]"}}]
    w.gh.check_runs[("sha1", POLICY_CHECK)] = {"status": "in_progress"}
    ready = await probe()
    assert ready.state == "partial" and "approved by the reviewer" in ready.detail
    assert "policy check in_progress" in ready.detail and "Approve and merge it yourself" in ready.detail

    w.gh.merge(pr)
    assert (await probe()).done


async def test_a_rehearsal_pr_closed_without_merging_is_not_success(w):
    await w.act("create_rehearsal_issue", repo="acme/shop")
    pr = w.gh.add_pr("agent/issue-1", "sha1")
    w.gh.prs[pr]["state"] = "closed"
    result = await w.probe("rehearsal", repo="acme/shop")
    assert result.state == "partial" and "closed without merging" in result.detail


async def test_the_secrets_step_is_still_done_in_a_new_session(tmp_path):
    """The Anthropic key is kept in memory only. Once the files exist, a new session must not ask for it again."""
    from onboarding.engine import Onboarding
    from onboarding.session import Session

    playbook, registry = packs.load("software-team")
    session = Session(None, playbook.name)
    for name, spec in playbook.inputs.items():
        if not spec.secret:
            session.answers[name] = spec.default or "x"
    session.answers.update(control_host="deploy@c", worker_host="deploy@w", deploy_dir="/opt/agent-company")
    (tmp_path / "infra" / "secrets").mkdir(parents=True)
    pack = packs.pack_module(packs.directory_of("software-team"))
    shells = FakeShells()
    everything = {k: "v" for which in pack.ENV_KEYS.values() for k in which}
    shells.when("sops --decrypt", out=json.dumps(everything))
    console = ScriptedConsole()
    ob = Onboarding(playbook, registry, session, Env(console, SecretStore(None), tmp_path, shells=shells))
    for name in ("control", "worker"):
        (tmp_path / "infra" / "secrets" / f"{name}.enc.env").write_text("ENC[...]")

    node = ob.resolve("secrets")
    assert (await ob.detect(node, fresh=True)).done and not console.prompts
    assert ob.missing(node) == ["anthropic_api_key"]  # only running the step needs it
    assert {"anthropic_api_key", "slack_bot_token"} <= set(ob.pending(node))  # and it is asked where it is used
    assert not ob.pb.inputs["anthropic_api_key"].persist and not ob.pb.inputs["slack_bot_token"].persist
