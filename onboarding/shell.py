"""Run commands on this machine or, over ssh, on a server. Always an argv list, never a local shell."""

import asyncio
import codecs
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

LOCAL = "local"
TAIL = 20_000  # keep the end of long output: that is where the error is
HOST_RE = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$")  # cannot start with "-"


@dataclass(frozen=True)
class Result:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    def problem(self) -> str:
        """One line saying what went wrong."""
        lines = (self.err.strip() or self.out.strip()).splitlines()
        return lines[-1][:300] if lines else f"exit code {self.code}"


class Shell:
    """`local` runs here; anything else is an ssh destination such as deploy@10.0.0.2."""

    def __init__(self, host: str):
        if host != LOCAL and not HOST_RE.match(host):
            raise ValueError(f"not a valid ssh destination: {host!r}")
        self.host = host

    @property
    def local(self) -> bool:
        return self.host == LOCAL

    def command(self, argv: list[str]) -> list[str]:
        if self.local:
            return argv
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, "--", shlex.join(argv)]

    async def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float = 120,
        on_line: Callable[[str], None] | None = None,
    ) -> Result:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command(argv),
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return Result(127, "", f"{self.command(argv)[0]}: command not found")

        async def pump(stream: asyncio.StreamReader, sink: list[str]) -> None:
            # Chunks, not lines: StreamReader.readline fails on a line longer than 64 KiB.
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            pending = ""
            while chunk := await stream.read(65536):
                text = decoder.decode(chunk)
                sink.append(text)
                if on_line:
                    *lines, pending = (pending + text).split("\n")
                    for line in lines:
                        on_line(line)
            if on_line and pending:
                on_line(pending)

        out: list[str] = []
        err: list[str] = []

        async def feed() -> None:
            assert proc.stdin is not None
            proc.stdin.write(stdin.encode() if stdin else b"")
            try:
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        async def finish() -> None:
            tasks = [pump(proc.stdout, out), pump(proc.stderr, err), proc.wait()]  # type: ignore[arg-type]
            if stdin is not None:
                tasks.append(feed())
            await asyncio.gather(*tasks)

        try:
            await asyncio.wait_for(finish(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return Result(124, "".join(out)[-TAIL:], f"timed out after {timeout:.0f}s")
        return Result(proc.returncode or 0, "".join(out)[-TAIL:], "".join(err)[-TAIL:])

    async def script(self, script: str, **kw) -> Result:
        """Run a bash script. Quote anything you interpolate into it."""
        return await self.run(["bash", "-c", script], **kw)
