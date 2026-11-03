"""The operator's terminal: output, questions and hidden input. A scripted one stands in for tests."""

import asyncio
import getpass
import threading
from collections.abc import Callable, Iterable


class Quit(Exception):
    """The operator ended the session (Ctrl-D or Ctrl-C at a prompt)."""


class Console:
    def say(self, text: str = "") -> None:
        raise NotImplementedError

    async def ask(self, prompt: str, *, secret: bool = False) -> str:
        raise NotImplementedError

    async def confirm(self, prompt: str, default: bool = False) -> bool:
        suffix = " [Y/n] " if default else " [y/N] "
        while True:
            answer = (await self.ask(prompt + suffix)).strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Please answer y or n.")


class TerminalConsole(Console):
    def say(self, text: str = "") -> None:
        print(text, flush=True)

    async def ask(self, prompt: str, *, secret: bool = False) -> str:
        # A daemon thread, so Ctrl-C is never held up by a prompt nobody will answer.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()

        def settle(setter, value) -> None:
            if not fut.done():
                setter(value)

        def read() -> None:
            try:
                value = getpass.getpass(prompt) if secret else input(prompt)
            except (EOFError, KeyboardInterrupt):
                loop.call_soon_threadsafe(settle, fut.set_exception, Quit())
            else:
                loop.call_soon_threadsafe(settle, fut.set_result, value)

        threading.Thread(target=read, daemon=True).start()
        return await fut


class ScriptedConsole(Console):
    """Answers from a list (a callable is called for its answer), and records everything.

    Raises Quit when the answers run out.
    """

    def __init__(self, answers: Iterable[str | Callable[[], str]] = ()):
        self.answers = list(answers)
        self.lines: list[str] = []
        self.prompts: list[tuple[str, bool]] = []  # (prompt, secret)

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    async def ask(self, prompt: str, *, secret: bool = False) -> str:
        self.prompts.append((prompt, secret))
        if not self.answers:
            raise Quit
        answer = self.answers.pop(0)
        return answer() if callable(answer) else answer

    @property
    def text(self) -> str:
        return "\n".join(self.lines)
