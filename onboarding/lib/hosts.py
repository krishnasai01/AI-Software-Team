"""Probes and a generic action for servers: ssh, docker, files, ports, containers.

A host is `local` (this machine) or an ssh destination. Everything runs as an argv list.
"""

import json
import shlex

import httpx

from onboarding.registry import ActionResult, Context, Registry, Status, done, missing, partial
from onboarding.shell import LOCAL


def where(host: str) -> str:
    return "this machine" if host == LOCAL else host


def register(reg: Registry) -> None:
    @reg.probe("ssh_ok")
    async def ssh_ok(ctx: Context, host: str) -> Status:
        if host == LOCAL:
            return done("this machine")
        result = await ctx.shell(host).run(["true"], timeout=20)
        if result.ok:
            return done(f"{host} reachable")
        return missing(f"cannot ssh to {host}: {result.problem()}. Run `ssh {host}` once by hand to accept its key.")

    @reg.probe("docker_ok")
    async def docker_ok(ctx: Context, host: str) -> Status:
        result = await ctx.shell(host).run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
        if not result.ok:
            return missing(f"docker is not usable on {where(host)}: {result.problem()}")
        if not (await ctx.shell(host).run(["docker", "compose", "version"], timeout=30)).ok:
            return partial(f"docker works on {where(host)} but the compose plugin is missing")
        return done(f"docker {result.out.strip()} on {where(host)}")

    @reg.probe("commands")
    async def commands(ctx: Context, host: str, names: list[str]) -> Status:
        absent = []
        for name in names:
            result = await ctx.shell(host).run(["sh", "-c", f"command -v {shlex.quote(name)}"], timeout=20)
            if not result.ok:
                absent.append(name)
        if not absent:
            return done()
        found = len(names) - len(absent)
        return (partial if found else missing)(f"{', '.join(absent)} not installed on {where(host)}")

    @reg.probe("docker_runtime")
    async def docker_runtime(ctx: Context, host: str, runtime: str) -> Status:
        result = await ctx.shell(host).run(["docker", "info", "--format", "{{json .Runtimes}}"], timeout=30)
        if not result.ok:
            return missing(f"docker is not usable on {where(host)}: {result.problem()}")
        try:
            runtimes = json.loads(result.out)
        except json.JSONDecodeError:
            return missing(f"could not read docker's runtimes on {where(host)}")
        if runtime in runtimes:
            return done(f"{runtime} is registered with docker on {where(host)}")
        return missing(f"docker on {where(host)} has no {runtime} runtime (has: {', '.join(runtimes) or 'none'})")

    @reg.probe("docker_images")
    async def docker_images(ctx: Context, host: str, images: list[str]) -> Status:
        absent = []
        for image in images:
            result = await ctx.shell(host).run(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=30)
            if not result.ok:
                absent.append(image)
        if not absent:
            return done(f"{len(images)} images present on {where(host)}")
        return (partial if len(absent) < len(images) else missing)(f"missing on {where(host)}: {', '.join(absent)}")

    @reg.probe("path_exists")
    async def path_exists(ctx: Context, host: str, path: str) -> Status:
        result = await ctx.shell(host).run(["test", "-e", path], timeout=20)
        return done(f"{path} exists on {where(host)}") if result.ok else missing(f"{path} not found on {where(host)}")

    @reg.probe("http_status")
    async def http_status(ctx: Context, url: str, expect: list[int]) -> Status:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
        except httpx.HTTPError as e:
            return missing(f"{url} is not reachable: {type(e).__name__}")
        if response.status_code in expect:
            return done(f"{url} answers ({response.status_code})")
        return missing(f"{url} answered {response.status_code}, expected {' or '.join(map(str, expect))}")

    @reg.probe("containers_up")
    async def containers_up(ctx: Context, host: str, project: str, services: list[str]) -> Status:
        fmt = '{{.Label "com.docker.compose.service"}}\t{{.Status}}'
        result = await ctx.shell(host).run(
            ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}", "--format", fmt], timeout=30
        )
        if not result.ok:
            return missing(f"cannot list containers on {where(host)}: {result.problem()}")
        up = {
            line.split("\t")[0]
            for line in result.out.splitlines()
            if "\t" in line and line.split("\t", 1)[1].startswith("Up") and "unhealthy" not in line
        }
        down = [s for s in services if s not in up]
        if not down:
            return done(f"{project}: {', '.join(services)} running")
        return (partial if up else missing)(f"{project} on {where(host)}: not running: {', '.join(down)}")

    def shell_preview(ctx: Context, host: str, script: str, timeout: int = 600) -> str:
        return f"On {where(host)}, run:\n{script}"

    @reg.action("shell", preview=shell_preview)
    async def shell(ctx: Context, host: str, script: str, timeout: int = 600) -> ActionResult:
        result = await ctx.shell(host).script(script, timeout=timeout, on_line=ctx.say)
        return ActionResult(result.ok, "completed" if result.ok else f"failed: {result.problem()}")
