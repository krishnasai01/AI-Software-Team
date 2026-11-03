"""Sandbox interface plus the Docker (gVisor) implementation.

A sandbox is a fresh, disposable container: no host mounts, egress only through the allowlist
proxy, and destroyed when the run ends. The task (including its short-lived credentials) goes in on
stdin, so secrets never appear in the container's environment, `docker inspect`, or argv. The runner
inside prints one `AGENT_RESULT <json>` line on stdout.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from core.settings import get_settings

RESULT_MARKER = "AGENT_RESULT "


@dataclass
class SandboxSpec:
    image: str
    task: dict
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: int | None = None
    name: str = field(default_factory=lambda: f"sbx-{uuid.uuid4().hex[:12]}")


@dataclass
class SandboxResult:
    exit_code: int
    result: dict | None  # parsed AGENT_RESULT, None if the runner never produced one
    timed_out: bool
    log_tail: str


class Sandbox(Protocol):
    async def run(self, spec: SandboxSpec, heartbeat: Callable[[], Awaitable[None]] | None = None) -> SandboxResult: ...


class DockerSandbox:
    def __init__(self, heartbeat_every_s: float = 30):
        self.s = get_settings()
        self.heartbeat_every_s = heartbeat_every_s

    def command(self, spec: SandboxSpec) -> list[str]:
        s = self.s
        cmd = [
            "docker",
            "run",
            "-i",
            "--rm",
            "--name",
            spec.name,
            "--runtime",
            s.sandbox_runtime,
            "--network",
            s.sandbox_network,
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
            "agent-company.sandbox=1",
            "-e",
            f"HTTPS_PROXY={s.sandbox_egress_proxy}",
            "-e",
            f"HTTP_PROXY={s.sandbox_egress_proxy}",
            "-e",
            f"https_proxy={s.sandbox_egress_proxy}",
            "-e",
            f"http_proxy={s.sandbox_egress_proxy}",
            "-e",
            "NO_PROXY=litellm,localhost,127.0.0.1",
            "-e",
            "no_proxy=litellm,localhost,127.0.0.1",
        ]
        for k, v in spec.env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd.append(spec.image)
        return cmd

    async def run(self, spec: SandboxSpec, heartbeat: Callable[[], Awaitable[None]] | None = None) -> SandboxResult:
        timeout = spec.timeout_s or self.s.sandbox_timeout_s
        proc = await asyncio.create_subprocess_exec(
            *self.command(spec),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps(spec.task).encode())
        await proc.stdin.drain()
        proc.stdin.close()

        lines: list[str] = []
        result: dict | None = None

        async def read() -> None:
            nonlocal result
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                if line.startswith(RESULT_MARKER):
                    result = json.loads(line[len(RESULT_MARKER) :])
                else:
                    lines.append(line)
                    del lines[:-200]
            await proc.wait()

        reader = asyncio.create_task(read())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        timed_out = False
        try:
            while not reader.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                await asyncio.wait({reader}, timeout=min(self.heartbeat_every_s, remaining))
                if heartbeat and not reader.done():
                    await heartbeat()
        finally:
            if not reader.done():
                await self.kill(spec.name)
                await reader
        return SandboxResult(proc.returncode or 0, result, timed_out, "\n".join(lines[-50:]))

    async def kill(self, name: str) -> None:
        p = await asyncio.create_subprocess_exec(
            "docker", "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await p.wait()
