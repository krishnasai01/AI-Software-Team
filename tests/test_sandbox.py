"""DockerSandbox against a real Docker daemon and the real base image.

Needs: docker build -f sandbox-images/base/Dockerfile -t agent-company/sandbox-base:test .
Runs with runc and the default bridge network (gVisor and the egress network are server config).
"""

import asyncio
import json
import shutil
import subprocess

import pytest

from core.sandbox import DockerSandbox, SandboxSpec

IMAGE = "agent-company/sandbox-base:test"


def _image_present() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _image_present(), reason=f"{IMAGE} not built")


@pytest.fixture
def sandbox(monkeypatch):
    from core.settings import get_settings

    monkeypatch.setenv("SANDBOX_RUNTIME", "runc")
    monkeypatch.setenv("SANDBOX_NETWORK", "bridge")
    monkeypatch.setenv("SANDBOX_EGRESS_PROXY", "http://127.0.0.1:9")  # nothing listens: no egress at all
    get_settings.cache_clear()
    yield DockerSandbox(heartbeat_every_s=0.5)
    get_settings.cache_clear()


def task(**over):
    t = {
        "run_id": "test-run",
        "role": "engineer",
        "repo": "acme/does-not-exist",
        "base_branch": "main",
        "branch": "agent/issue-1",
        "ticket": {"number": 1, "title": "t", "body": "b"},
        "secrets": {"github_token": "ghs_SECRET_TOKEN", "model_base_url": "http://x", "model_api_key": "sk-run"},
    }
    t.update(over)
    return t


async def test_runner_always_reports_a_result_and_never_leaks_secrets(sandbox):
    beats = 0

    async def heartbeat():
        nonlocal beats
        beats += 1

    res = await sandbox.run(SandboxSpec(image=IMAGE, task=task(), timeout_s=120), heartbeat=heartbeat)
    assert not res.timed_out
    assert res.result is not None and res.result["outcome"] == "error"
    assert "clone failed" in res.result["errors"][0]
    blob = json.dumps(res.result) + res.log_tail
    assert "ghs_SECRET_TOKEN" not in blob and "sk-run" not in blob


async def test_container_is_hardened_and_removed(sandbox):
    spec = SandboxSpec(image=IMAGE, task=task())
    cmd = sandbox.command(spec)
    assert "--cap-drop" in cmd and "ALL" in cmd and "no-new-privileges" in cmd
    assert "ghs_SECRET_TOKEN" not in " ".join(cmd)

    run = asyncio.create_task(sandbox.run(spec))
    for _ in range(100):
        inspect = subprocess.run(["docker", "inspect", spec.name], capture_output=True, text=True)
        if inspect.returncode == 0:
            assert "ghs_SECRET_TOKEN" not in inspect.stdout  # secrets came over stdin, not env
            info = json.loads(inspect.stdout)[0]
            assert info["Config"]["User"] == "agent"
            assert info["HostConfig"]["Binds"] in (None, [])
            break
        await asyncio.sleep(0.05)
    await run
    gone = subprocess.run(["docker", "inspect", spec.name], capture_output=True)
    assert gone.returncode != 0  # --rm


async def test_wall_clock_timeout_kills_the_container(sandbox, monkeypatch):
    spec = SandboxSpec(image=IMAGE, task=task(), timeout_s=2)
    # Replace the entrypoint with a sleeper to simulate a runaway agent.
    real = sandbox.command
    monkeypatch.setattr(sandbox, "command", lambda s: [*real(s)[:-1], "--entrypoint", "sleep", s.image, "300"])
    res = await sandbox.run(spec)
    assert res.timed_out and res.result is None
    assert subprocess.run(["docker", "inspect", spec.name], capture_output=True).returncode != 0
