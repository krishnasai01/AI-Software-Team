"""Sandbox interface plus the Docker (gVisor) implementation.

A sandbox is a fresh, disposable container with no host mounts and no GitHub credentials. Its
inputs (task file and repo bundle) are copied in before it starts; its outputs (/out) are copied
out after it stops. Each job gets its own internal network with only the model and egress proxies
attached, so sandboxes cannot reach the internet directly or each other.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from core.settings import get_settings

LABEL = "agent-company.sandbox"
LOG_TAIL_CHARS = 20_000


@dataclass
class SandboxSpec:
    image: str
    name: str
    task_dir: Path  # copied to /task (task.json, repo.bundle)
    out_dir: Path  # receives /out after the run
    timeout_s: int
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxResult:
    exit_code: int | None
    timed_out: bool
    lost_lease: bool
    log_tail: str


# Returns False when the job's lease is gone (cancelled or taken over): the run is killed.
Heartbeat = Callable[[], Awaitable[bool]]


class Sandbox(Protocol):
    async def run(self, spec: SandboxSpec, heartbeat: Heartbeat | None = None) -> SandboxResult: ...


async def _docker(*args: str, check: bool = True) -> str:
    p = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await p.communicate()
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {err.decode(errors='replace').strip()[-500:]}")
    return out.decode(errors="replace").strip()


class DockerSandbox:
    def __init__(self, heartbeat_every_s: float = 30):
        self.s = get_settings()
        self.heartbeat_every_s = heartbeat_every_s

    def create_args(self, spec: SandboxSpec, network: str) -> list[str]:
        s = self.s
        args = [
            "create",
            "--name",
            spec.name,
            "--runtime",
            s.sandbox_runtime,
            "--network",
            network,
            "--cpus",
            str(s.sandbox_cpus),
            "--memory",
            s.sandbox_memory,
            "--pids-limit",
            "1024",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--label",
            f"{LABEL}=1",
            "--label",
            f"{LABEL}.worker={s.worker_id}",
        ]
        if s.sandbox_disk_quota:
            args += ["--storage-opt", f"size={s.sandbox_disk_quota}"]
        env = {
            "HTTPS_PROXY": s.sandbox_egress_proxy,
            "HTTP_PROXY": s.sandbox_egress_proxy,
            "https_proxy": s.sandbox_egress_proxy,
            "http_proxy": s.sandbox_egress_proxy,
            "NO_PROXY": "litellm,localhost,127.0.0.1",
            "no_proxy": "litellm,localhost,127.0.0.1",
            **spec.env,
        }
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        return [*args, spec.image]

    async def _network(self, name: str) -> tuple[str, bool]:
        if self.s.sandbox_network != "per-job":
            return self.s.sandbox_network, False
        net = f"{name}-net"
        await _docker(
            "network",
            "create",
            "--internal",
            "--label",
            f"{LABEL}=1",
            "--label",
            f"{LABEL}.worker={self.s.worker_id}",
            net,
        )
        for alias, container in self.s.sandbox_proxy_containers.items():
            await _docker("network", "connect", "--alias", alias, net, container)
        return net, True

    async def _drop_network(self, net: str) -> None:
        for container in self.s.sandbox_proxy_containers.values():
            await _docker("network", "disconnect", "--force", net, container, check=False)
        await _docker("network", "rm", net, check=False)

    async def run(self, spec: SandboxSpec, heartbeat: Heartbeat | None = None) -> SandboxResult:
        net, owned = await self._network(spec.name)
        try:
            await _docker(*self.create_args(spec, net))
            await _docker("cp", f"{spec.task_dir}/.", f"{spec.name}:/task")
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "start",
                "--attach",
                spec.name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout
            tail = ""

            async def read() -> None:
                nonlocal tail
                while chunk := await proc.stdout.read(65536):  # chunks, not lines: no line-length limit
                    tail = (tail + chunk.decode(errors="replace"))[-LOG_TAIL_CHARS:]
                await proc.wait()

            reader = asyncio.create_task(read())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + spec.timeout_s
            timed_out = lost = False
            while not reader.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                await asyncio.wait({reader}, timeout=min(self.heartbeat_every_s, remaining))
                if heartbeat and not reader.done() and not await heartbeat():
                    lost = True
                    break
            if not reader.done():
                await _docker("kill", spec.name, check=False)
            await reader
            spec.out_dir.mkdir(parents=True, exist_ok=True)
            await _docker("cp", f"{spec.name}:/out/.", str(spec.out_dir), check=False)
            code = await _docker("inspect", "--format", "{{.State.ExitCode}}", spec.name, check=False)
            return SandboxResult(int(code) if code.lstrip("-").isdigit() else None, timed_out, lost, tail)
        finally:
            await _docker("rm", "--force", spec.name, check=False)
            if owned:
                await self._drop_network(net)

    async def cleanup_orphans(self) -> int:
        """Remove sandboxes and networks this worker left behind (crash or restart)."""
        flt = f"label={LABEL}.worker={self.s.worker_id}"
        ids = (await _docker("ps", "-aq", "--filter", flt, check=False)).split()
        if ids:
            await _docker("rm", "--force", *ids, check=False)
        for net in (await _docker("network", "ls", "-q", "--filter", flt, check=False)).split():
            await self._drop_network(net)
        return len(ids)
