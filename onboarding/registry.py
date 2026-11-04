"""What a playbook's steps refer to: probes (is it done?) and actions (do it), and the context they get."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from onboarding.console import Console
from onboarding.playbook import Target
from onboarding.secrets import SecretStore
from onboarding.shell import Shell

DONE, MISSING, PARTIAL, UNKNOWN = "done", "missing", "partial", "unknown"


@dataclass(frozen=True)
class Status:
    state: str  # done | missing | partial | unknown
    detail: str = ""

    @property
    def done(self) -> bool:
        return self.state == DONE


def done(detail: str = "") -> Status:
    return Status(DONE, detail)


def missing(detail: str) -> Status:
    return Status(MISSING, detail)


def partial(detail: str) -> Status:
    return Status(PARTIAL, detail)


def unknown(detail: str) -> Status:
    return Status(UNKNOWN, detail)


def combine(statuses: list[Status]) -> Status:
    """A step with several probes is done only when every one is done."""
    if all(s.done for s in statuses):
        return Status(DONE, "; ".join(s.detail for s in statuses if s.detail))
    detail = "; ".join(s.detail for s in statuses if not s.done and s.detail)
    states = {s.state for s in statuses}
    if UNKNOWN in states:
        return Status(UNKNOWN, detail)
    if DONE in states or PARTIAL in states:
        return Status(PARTIAL, detail)
    return Status(MISSING, detail)


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    summary: str  # what the model and the log see: never put a secret in it


class MissingInput(Exception):
    pass


@dataclass
class Env:
    """Everything outside the playbook that probes and actions may touch."""

    console: Console
    secrets: SecretStore
    root: Path  # this repo's root: templates and infra files live here
    shells: Callable[[str], Shell] = Shell
    services: dict[str, Any] = field(default_factory=dict)  # replace real clients (tests)
    obtain: Callable[[str], Awaitable[str | None]] | None = None  # set by the engine: a secret, asking if needed


@dataclass
class Context:
    env: Env
    values: Mapping[str, str]  # non-secret inputs: the team's, plus this target's
    target: Target | None = None
    interactive: bool = False  # actions may ask the operator for a missing secret; probes never do

    @property
    def root(self) -> Path:
        return self.env.root

    @property
    def secrets(self) -> SecretStore:
        return self.env.secrets

    def shell(self, host: str) -> Shell:
        return self.env.shells(host)

    def say(self, text: str) -> None:
        """Progress for the operator's terminal (never for the model)."""
        self.env.console.say(self.env.secrets.redact(text))

    async def secret(self, name: str) -> str:
        value = self.env.secrets.get(name)
        if value is None and self.interactive and self.env.obtain is not None:
            value = await self.env.obtain(name)
        if not value:
            raise MissingInput(f"needs {name}")
        return value

    def value(self, name: str) -> str:
        if name not in self.values:
            raise MissingInput(f"needs {name}")
        return self.values[name]


Probe = Callable[..., Awaitable[Status]]


@dataclass(frozen=True)
class Action:
    name: str
    run: Callable[..., Awaitable[ActionResult]]
    preview: Callable[..., Awaitable[str] | str] | None = None  # exactly what will happen, without doing it


class Registry:
    def __init__(self) -> None:
        self.probes: dict[str, Probe] = {}
        self.actions: dict[str, Action] = {}

    def probe(self, name: str) -> Callable[[Probe], Probe]:
        def deco(fn: Probe) -> Probe:
            if name in self.probes:
                raise ValueError(f"probe {name!r} registered twice")
            self.probes[name] = fn
            return fn

        return deco

    def action(self, name: str, preview: Callable[..., Awaitable[str] | str] | None = None):
        def deco(fn: Callable[..., Awaitable[ActionResult]]):
            if name in self.actions:
                raise ValueError(f"action {name!r} registered twice")
            self.actions[name] = Action(name, fn, preview)
            return fn

        return deco
