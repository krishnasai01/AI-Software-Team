"""DockerSandbox mechanics against a real Docker daemon and the real base image."""

import asyncio
import json
import shutil
import subprocess

import pytest

from core.sandbox import DockerSandbox, SandboxSpec

IMAGE = "agent-company/sandbox-base:test"


def _image_present() -> bool:
    return (
        bool(shutil.which("docker"))
        and subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
    )


pytestmark = pytest.mark.skipif(not _image_present(), reason=f"{IMAGE} not built")


@pytest.fixture
def sandbox(monkeypatch):
    from core.settings import get_settings

    monkeypatch.setenv("SANDBOX_RUNTIME", "runc")
    monkeypatch.setenv("SANDBOX_NETWORK", "none")
    monkeypatch.setenv("WORKER_ID", "pytest-worker")
    get_settings.cache_clear()
    yield DockerSandbox(heartbeat_every_s=0.3)
    get_settings.cache_clear()


def scripted(sb: DockerSandbox, monkeypatch, script: str) -> None:
    """Replace the entrypoint with a shell script (the container is otherwise unchanged)."""
    real = sb.create_args
    monkeypatch.setattr(
        sb, "create_args", lambda spec, net: [*real(spec, net)[:-1], "--entrypoint", "sh", spec.image, "-c", script]
    )


def spec(tmp_path, name, timeout=60):
    task = tmp_path / "task"
    task.mkdir(exist_ok=True)
    (task / "task.json").write_text(json.dumps({"hello": "world"}))
    return SandboxSpec(image=IMAGE, name=name, task_dir=task, out_dir=tmp_path / "out", timeout_s=timeout)


def gone(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode != 0


async def test_inputs_in_outputs_out_and_hardening(sandbox, tmp_path, monkeypatch):
    # A 1 MB single-line output and a big result file: no line-length limit anywhere.
    scripted(
        sandbox,
        monkeypatch,
        "cat /task/task.json > /out/result.json; head -c 1000000 /dev/zero | tr '\\0' x; "
        "id -u > /out/uid; touch /task/x 2>/dev/null || echo ro > /out/task_ro",
    )
    s = spec(tmp_path, "sbx-pytest-io")
    args = sandbox.create_args(s, "none")
    assert "--cap-drop" in args and "ALL" in args and "no-new-privileges" in args and "-v" not in args
    res = await sandbox.run(s)
    assert res.exit_code == 0 and not res.timed_out
    assert json.loads((s.out_dir / "result.json").read_text()) == {"hello": "world"}
    assert (s.out_dir / "uid").read_text().strip() == "10001"  # never root
    assert (s.out_dir / "task_ro").exists()  # the agent cannot rewrite its own task
    assert len(res.log_tail) == 20_000 and gone(s.name)


async def test_wall_clock_timeout_kills_the_container(sandbox, tmp_path, monkeypatch):
    scripted(sandbox, monkeypatch, "sleep 300")
    s = spec(tmp_path, "sbx-pytest-timeout", timeout=2)
    res = await sandbox.run(s)
    assert res.timed_out and gone(s.name)


async def test_lost_lease_kills_the_container(sandbox, tmp_path, monkeypatch):
    scripted(sandbox, monkeypatch, "sleep 300")
    s = spec(tmp_path, "sbx-pytest-lease")
    beats = 0

    async def heartbeat():
        nonlocal beats
        beats += 1
        return beats < 2  # the job gets cancelled after the first beat

    res = await asyncio.wait_for(sandbox.run(s, heartbeat), timeout=60)
    assert res.lost_lease and not res.timed_out and gone(s.name)


async def test_orphans_from_a_previous_worker_run_are_removed(sandbox, tmp_path):
    name = "sbx-pytest-orphan"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            "agent-company.sandbox=1",
            "--label",
            "agent-company.sandbox.worker=pytest-worker",
            "--entrypoint",
            "sleep",
            IMAGE,
            "300",
        ],
        check=True,
        capture_output=True,
    )
    assert await sandbox.cleanup_orphans() >= 1
    assert gone(name)


@pytest.fixture
def proxies(monkeypatch):
    from core.settings import get_settings

    names = {"litellm": "pytest-litellm", "egress": "pytest-egress"}
    for n in names.values():
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", n, "--entrypoint", "sleep", IMAGE, "300"], check=True, capture_output=True
        )
    monkeypatch.setenv("SANDBOX_NETWORK", "per-job")
    monkeypatch.setenv("SANDBOX_PROXY_CONTAINERS", json.dumps(names))
    get_settings.cache_clear()
    yield names
    for n in names.values():
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)


async def test_per_job_network_reaches_only_the_proxies(sandbox, proxies, tmp_path, monkeypatch):
    sb = DockerSandbox(heartbeat_every_s=0.3)
    scripted(
        sb,
        monkeypatch,
        "getent hosts litellm egress > /out/dns; "
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\" "
        "&& echo reachable > /out/internet || echo blocked > /out/internet",
    )
    s = spec(tmp_path, "sbx-pytest-net")
    res = await sb.run(s)
    assert res.exit_code == 0
    assert len((s.out_dir / "dns").read_text().splitlines()) == 2  # both proxies resolve by alias
    assert (s.out_dir / "internet").read_text().strip() == "blocked"
    nets = subprocess.run(
        ["docker", "network", "ls", "-q", "--filter", "name=sbx-pytest-net"], capture_output=True, text=True
    ).stdout.strip()
    assert nets == ""  # the job's network is gone, and the proxies were detached from it
