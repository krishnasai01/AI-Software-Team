import httpx
import pytest

from onboarding.console import ScriptedConsole
from onboarding.lib import hosts
from onboarding.registry import Context, Env, Registry
from onboarding.secrets import SecretStore
from tests.onboarding_fakes import FakeShells


@pytest.fixture
def lib(tmp_path):
    reg = Registry()
    hosts.register(reg)
    shells = FakeShells()
    console = ScriptedConsole()
    ctx = Context(Env(console, SecretStore(None), tmp_path, shells=shells), {})
    return reg, ctx, shells, console


async def test_ssh_ok(lib):
    reg, ctx, shells, _ = lib
    assert (await reg.probes["ssh_ok"](ctx, host="local")).done and not shells.calls  # nothing to connect to
    assert (await reg.probes["ssh_ok"](ctx, host="deploy@10.0.0.2")).done
    shells.when("true", err="Host key verification failed.", code=255)
    down = await reg.probes["ssh_ok"](ctx, host="deploy@10.0.0.3")
    assert down.state == "missing" and "Host key verification failed" in down.detail
    assert "ssh deploy@10.0.0.3" in down.detail  # says what to do about it


async def test_docker_needs_the_engine_and_the_compose_plugin(lib):
    reg, ctx, shells, _ = lib
    shells.when("docker compose version", err="unknown command", code=1)
    shells.when("docker info", out="27.1\n")
    partial = await reg.probes["docker_ok"](ctx, host="local")
    assert partial.state == "partial" and "compose plugin" in partial.detail
    shells.rules.clear()
    shells.when("docker info", err="permission denied while trying to connect", code=1)
    denied = await reg.probes["docker_ok"](ctx, host="local")
    assert denied.state == "missing" and "permission denied" in denied.detail
    shells.rules.clear()
    shells.when("docker info", out="27.1\n")
    assert (await reg.probes["docker_ok"](ctx, host="local")).done


async def test_commands_reports_which_are_missing(lib):
    reg, ctx, shells, _ = lib
    shells.when("sops", code=1)
    result = await reg.probes["commands"](ctx, host="local", names=["git", "sops"])
    assert result.state == "partial" and result.detail == "sops not installed on this machine"
    shells.when("git", code=1)
    assert (await reg.probes["commands"](ctx, host="local", names=["git", "sops"])).state == "missing"
    shells.rules.clear()
    assert (await reg.probes["commands"](ctx, host="local", names=["git", "sops"])).done


async def test_docker_runtime(lib):
    reg, ctx, shells, _ = lib
    shells.when("json .Runtimes", out='{"runc": {"path": "runc"}}')
    missing = await reg.probes["docker_runtime"](ctx, host="deploy@w", runtime="runsc")
    assert missing.state == "missing" and "has: runc" in missing.detail
    shells.rules.clear()
    shells.when("json .Runtimes", out='{"runc": {}, "runsc": {"path": "/usr/local/bin/runsc"}}')
    assert (await reg.probes["docker_runtime"](ctx, host="deploy@w", runtime="runsc")).done
    shells.rules.clear()
    shells.when("json .Runtimes", out="not json")
    assert (await reg.probes["docker_runtime"](ctx, host="deploy@w", runtime="runsc")).state == "missing"


async def test_docker_images(lib):
    reg, ctx, shells, _ = lib
    shells.when("image inspect --format {{.Id}} b", code=1)
    result = await reg.probes["docker_images"](ctx, host="local", images=["a", "b"])
    assert result.state == "partial" and "b" in result.detail and "a" not in result.detail.split(": ")[1]
    shells.when("image inspect --format {{.Id}} a", code=1)
    assert (await reg.probes["docker_images"](ctx, host="local", images=["a", "b"])).state == "missing"


async def test_path_exists(lib):
    reg, ctx, shells, _ = lib
    assert (await reg.probes["path_exists"](ctx, host="local", path="/x")).done
    shells.when("test -e /gone", code=1)
    assert (await reg.probes["path_exists"](ctx, host="local", path="/gone")).state == "missing"


CONTAINERS = "\n".join(
    [
        "postgres\tUp 5 minutes (healthy)",
        "webhook\tUp 5 minutes",
        "reconciler\tUp 2 seconds (unhealthy)",
        "caddy\tExited (1) 1m ago",
    ]
)


async def test_containers_up_counts_only_running_healthy_ones(lib):
    reg, ctx, shells, _ = lib
    shells.when("docker ps", out=CONTAINERS)
    result = await reg.probes["containers_up"](
        ctx, host="local", project="p", services=["postgres", "webhook", "reconciler", "caddy"]
    )
    assert result.state == "partial" and result.detail.endswith("not running: reconciler, caddy")
    assert (await reg.probes["containers_up"](ctx, host="local", project="p", services=["postgres", "webhook"])).done
    shells.rules.clear()
    shells.when("docker ps", out="")
    assert (await reg.probes["containers_up"](ctx, host="local", project="p", services=["postgres"])).state == "missing"
    assert "label=com.docker.compose.project=p" in " ".join(shells.calls[-1].argv)


async def test_http_status(lib, monkeypatch):
    reg, ctx, _, _ = lib
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "down.example":
            raise httpx.ConnectError("refused")
        return httpx.Response(405 if request.url.path == "/webhooks/github" else 404)

    monkeypatch.setattr(hosts.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    ok = await reg.probes["http_status"](ctx, url="https://hooks.example/webhooks/github", expect=[405])
    assert ok.done
    wrong = await reg.probes["http_status"](ctx, url="https://hooks.example/other", expect=[405])
    assert wrong.state == "missing" and "answered 404, expected 405" in wrong.detail
    down = await reg.probes["http_status"](ctx, url="https://down.example/x", expect=[405])
    assert down.state == "missing" and "ConnectError" in down.detail


async def test_the_generic_shell_action_previews_streams_and_reports(lib):
    reg, ctx, shells, console = lib
    action = reg.actions["shell"]
    assert "On deploy@w, run:\nmake all" == action.preview(ctx, host="deploy@w", script="make all")
    assert action.preview(ctx, host="local", script="x").startswith("On this machine")
    shells.when("make all", err="make: *** no rule\n", code=2)
    failed = await action.run(ctx, host="deploy@w", script="make all")
    assert not failed.ok and "no rule" in failed.summary
    assert (await action.run(ctx, host="local", script="true")).ok
