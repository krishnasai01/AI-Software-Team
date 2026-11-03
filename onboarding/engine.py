"""The engine: what is done, what is ready, what is blocked, and the only way anything gets run.

Everything that matters is decided here, in code the model cannot change: a step is done only when
its probes say so, an action runs only after the operator's own yes at the terminal, and secrets are
asked for with hidden input and never handed to the model.
"""

import inspect
import os
import re
import textwrap
from dataclasses import dataclass
from time import monotonic

from onboarding.console import Quit
from onboarding.playbook import (
    TARGET_NAME,
    InputSpec,
    MissingValue,
    Playbook,
    StepSpec,
    Target,
    render,
    render_text,
)
from onboarding.registry import (
    DONE,
    ActionResult,
    Context,
    Env,
    MissingInput,
    Registry,
    Status,
    combine,
    missing,
    partial,
    unknown,
)
from onboarding.session import Session

READY, BLOCKED, SKIPPED, NOT_NEEDED = "ready", "blocked", "skipped", "n/a"
CACHE_TTL = 30.0  # seconds a probe result is reused; any change by the operator clears it


class EngineError(Exception):
    """A request the engine refuses. The message says why, for the operator or the model."""


@dataclass(frozen=True)
class Node:
    """One concrete step: a step, and the target it is for (if it repeats per target)."""

    step: StepSpec
    target: Target | None = None

    @property
    def key(self) -> str:
        return self.step.id if self.target is None else f"{self.step.id}@{self.target.name}"


@dataclass(frozen=True)
class Item:
    node: Node
    state: str  # done | ready | blocked | skipped | n/a
    status: Status
    blocked_by: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.state in (DONE, SKIPPED, NOT_NEEDED)


@dataclass(frozen=True)
class RunOutcome:
    ran: bool  # False when declined or refused
    ok: bool
    message: str  # redacted: safe for the model and the log
    status: Status | None = None


def scrub_url(value: str) -> str:
    """Drop credentials from a URL: a git remote may embed a token."""
    return re.sub(r"^(https?://)[^/@\s]+@", r"\1", value)


class Onboarding:
    def __init__(self, playbook: Playbook, registry: Registry, session: Session, env: Env):
        self.pb = playbook
        self.registry = registry
        self.session = session
        self.env = env
        self.env.obtain = self.obtain
        self._cache: dict[str, tuple[float, Status]] = {}

    # Nodes

    def nodes(self) -> list[Node]:
        targets = self.session.all_targets()
        out: list[Node] = []
        for step in self.pb.steps:
            if step.per:
                out += [Node(step, t) for t in targets if t.kind in step.per]
            else:
                out.append(Node(step))
        return out

    def resolve(self, ref: str) -> Node:
        ref = ref.strip()
        nodes = self.nodes()
        if exact := [n for n in nodes if n.key == ref]:
            return exact[0]
        same = [n for n in nodes if n.step.id == ref]
        if len(same) == 1:
            return same[0]
        if same:
            raise EngineError(f"{ref} repeats per target; say which: " + ", ".join(n.key for n in same))
        if any(s.id == ref for s in self.pb.steps):
            raise EngineError(f"{ref} repeats per {'/'.join(self.pb.step(ref).per)}; add one first")
        raise EngineError(f"no step {ref!r}. Steps: " + ", ".join(n.key for n in nodes))

    def needs_of(self, node: Node, nodes: list[Node]) -> list[str]:
        keys: list[str] = []
        for need in node.step.needs:
            needed = self.pb.step(need)
            instances = [n.key for n in nodes if n.step.id == need]
            if not needed.per:
                keys.append(need)
            elif node.target and node.target.kind in needed.per:
                keys.append(f"{need}@{node.target.name}")
            else:
                keys.extend(instances)  # a barrier: every instance
        return keys

    # Values

    def values(self, node: Node | None = None) -> dict[str, str]:
        """The non-secret inputs as probes and templates see them: defaults, then answers."""
        v = {n: s.default for n, s in self.pb.inputs.items() if s.default is not None and not s.secret}
        v.update(
            {n: a for n, a in self.session.answers.items() if not (n in self.pb.inputs and self.pb.inputs[n].secret)}
        )
        if node is not None and node.target is not None:
            spec = self.pb.targets[node.target.kind]
            v.update({n: s.default for n, s in spec.inputs.items() if s.default is not None})
            v.update(node.target.values)
            for n in spec.inputs:
                v.setdefault(n, "")
            v["target"], v["kind"] = node.target.name, node.target.kind
        if node is not None:
            for n in node.step.inputs:
                if not self.spec_for(node, n)[0].secret:
                    v.setdefault(n, "")
        return v

    def spec_for(self, node: Node | None, name: str) -> tuple[InputSpec, str]:
        if node is not None and node.target is not None and name in self.pb.targets[node.target.kind].inputs:
            return self.pb.targets[node.target.kind].inputs[name], "target"
        if name in self.pb.inputs:
            return self.pb.inputs[name], "team"
        raise EngineError(f"{node.key if node else 'the playbook'} has no input {name!r}")

    def answered(self, node: Node | None, name: str) -> bool:
        spec, scope = self.spec_for(node, name)
        if spec.secret:
            return self.env.secrets.has(name) or self.session.answers.get(name) == ""
        if scope == "target":
            assert node is not None and node.target is not None
            return name in node.target.values
        return name in self.session.answers

    def pending(self, node: Node) -> list[str]:
        """Inputs still to ask: required ones, and optional ones nobody has been asked about yet."""
        out = []
        for name in node.step.inputs:
            spec, _ = self.spec_for(node, name)
            if spec.generate or (spec.optional and spec.default is not None):
                continue
            if not self.answered(node, name):
                out.append(name)
        return out

    def missing(self, node: Node) -> list[str]:
        return [n for n in self.pending(node) if not self.spec_for(node, n)[0].optional]

    def title(self, node: Node) -> str:
        try:
            return render_text(node.step.title, self.values(node))
        except MissingValue:
            return node.step.title

    def context(self, node: Node | None, interactive: bool = False) -> Context:
        return Context(self.env, self.values(node), node.target if node else None, interactive)

    # Asking

    async def suggestion(self, spec: InputSpec) -> tuple[str, str] | None:
        for source in spec.suggest:
            if "env" in source and (value := os.environ.get(source["env"], "").strip()):
                return scrub_url(value), f"${source['env']}"
            if "cmd" in source:
                result = await self.env.shells("local").run(list(source["cmd"]), timeout=10)
                if result.ok and result.out.strip():
                    return scrub_url(result.out.strip().splitlines()[0]), " ".join(source["cmd"])
        return None

    async def prompt(self, spec: InputSpec, current: str | None = None) -> str | None:
        """Ask until the answer is acceptable. "" means an optional input was left empty; None, keep the old one."""
        console = self.env.console
        if spec.secret and not self.env.secrets.has(spec.name) and spec.suggest:
            found = await self.suggestion(spec)
            if found and await console.confirm(f"Use the {spec.prompt.lower()} from {found[1]}?", default=True):
                return found[0]
        if spec.help:
            console.say(spec.help)
        shown = current
        if shown is None and not spec.secret and spec.suggest and (found := await self.suggestion(spec)):
            shown = found[0]
        shown = shown or spec.default
        label = spec.prompt + (f" ({'/'.join(spec.options)})" if spec.kind == "choice" else "")
        label += " (optional, Enter to skip)" if spec.optional and not shown else ""
        while True:
            if spec.secret:
                kept = " (Enter keeps the current one)" if self.env.secrets.has(spec.name) else ""
                raw = await console.ask(f"{label}{kept} (hidden): ", secret=True)
            else:
                raw = await console.ask(f"{label}{f' [{shown}]' if shown else ''}: ")
            value = raw.strip()
            if not value:
                if spec.secret and self.env.secrets.has(spec.name):
                    return None
                value = shown or ""
            if not value:
                if spec.optional:
                    return ""
                console.say("A value is needed.")
                continue
            if problem := spec.problem(value):
                console.say(problem)
                continue
            return value

    def store(self, node: Node | None, name: str, value: str) -> None:
        spec, scope = self.spec_for(node, name)
        if spec.secret:
            if value:
                self.env.secrets.set(name, value, spec.persist)
            else:
                self.session.answers[name] = ""  # "asked, left empty": not a secret
        elif scope == "team":
            self.session.answers[name] = value
        else:
            assert node is not None and node.target is not None
            self.session.targets[node.target.kind][node.target.name][name] = value
        self.session.save()
        self._cache.clear()

    def fill_generated(self, node: Node | None) -> None:
        for name in node.step.inputs if node else ():
            spec, _ = self.spec_for(node, name)
            if spec.generate and not self.env.secrets.has(name):
                self.env.secrets.generate(name, spec.persist)

    async def ask(self, node: Node | None, name: str) -> str:
        """Ask the operator for one input and store it. Returns what the model may see."""
        spec, _ = self.spec_for(node, name)
        if spec.generate:
            if not self.env.secrets.has(name):
                self.env.secrets.generate(name, spec.persist)
            return "generated (hidden)"
        current = None if spec.secret else self.values(node).get(name) or None
        value = await self.prompt(spec, current)
        if value is None:
            return "kept the current value"
        self.store(node, name, value)
        return "set (hidden)" if spec.secret and value else (value or "(left empty)")

    async def ask_pending(self, node: Node) -> list[str]:
        lines = []
        for name in self.pending(node):
            lines.append(f"{name}: {await self.ask(node, name)}")
        return lines

    async def obtain(self, name: str) -> str | None:
        """A secret by name, asking for it if needed. Used by probes and actions through ctx.secret()."""
        if self.env.secrets.has(name):
            return self.env.secrets.get(name)
        spec = self.pb.inputs.get(name)
        if spec is None or not spec.secret:
            return None
        if spec.generate:
            return self.env.secrets.generate(name, spec.persist)
        await self.ask(None, name)
        return self.env.secrets.get(name)

    async def add_target(self, kind: str) -> Target:
        spec = self.pb.targets.get(kind)
        if spec is None:
            raise EngineError(f"no target kind {kind!r}. Kinds: {', '.join(self.pb.targets) or 'none'}")
        have = self.session.targets.get(kind, {})
        if spec.max is not None and len(have) >= spec.max:
            raise EngineError(f"at most {spec.max} {spec.title} can be added")
        console = self.env.console
        taken = self.session.target_names()
        default = spec.default_name if spec.default_name not in taken else None
        while True:
            raw = await console.ask(
                f"Name for this {spec.title} (lowercase letters, digits, dashes){f' [{default}]' if default else ''}: "
            )
            name = raw.strip() or (default or "")
            if not TARGET_NAME.fullmatch(name):
                console.say("Use a short lowercase name: letters, digits and dashes, starting with a letter.")
            elif name in taken:
                console.say(f"{name!r} is already used.")
            else:
                break
        values: dict[str, str] = {}
        for input_name, ispec in spec.inputs.items():
            if ispec.generate or (ispec.optional and ispec.default is not None):
                continue
            value = await self.prompt(ispec)
            if value is not None:
                values[input_name] = value
        self.session.targets.setdefault(kind, {})[name] = values
        self.session.record("added target", kind=kind, name=name)
        self._cache.clear()
        return Target(kind, name, values)

    # Detecting and planning

    async def detect(self, node: Node, fresh: bool = False) -> Status:
        cached = self._cache.get(node.key)
        if cached and not fresh and monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
        raw = await self._detect(node)
        status = Status(raw.state, self.env.secrets.redact(raw.detail))
        self._cache[node.key] = (monotonic(), status)
        return status

    async def _detect(self, node: Node) -> Status:
        step = node.step
        if step.collect:
            spec = self.pb.targets[step.collect]
            have = len(self.session.targets.get(step.collect, {}))
            if have >= spec.min:
                return Status(DONE, f"{have} added" if have else "")
            return missing(f"add {spec.min - have} more {spec.title}")
        # A secret is never part of a probe's arguments, so one that is only needed to run the action
        # (kept in memory, gone in the next session) must not make a finished step look unfinished.
        if needed := [n for n in self.missing(node) if not self.spec_for(node, n)[0].secret]:
            return missing("needs: " + ", ".join(needed))
        ctx = self.context(node)
        statuses = []
        for call in step.detect:
            try:
                statuses.append(await self.registry.probes[call.name](ctx, **render(call.args, ctx.values)))
            except Quit:
                raise
            except MissingValue as e:
                statuses.append(missing(f"needs {e.args[0]}"))
            except MissingInput as e:
                statuses.append(missing(str(e)))
            except Exception as e:  # noqa: BLE001  a broken probe must not stop the checklist
                statuses.append(unknown(f"{call.name} failed: {type(e).__name__}: {e}"))
        status = combine(statuses)
        if step.ack and status.done and node.key not in self.session.acks:
            return partial("waiting for your confirmation") if step.detect else missing("waiting for your confirmation")
        return status

    def applies(self, step: StepSpec) -> bool:
        values = self.values()
        return all(values.get(k) == v for k, v in step.when.items())

    async def evaluate(self, fresh: bool = False) -> list[Item]:
        if fresh:
            self._cache.clear()
        nodes = self.nodes()
        items: dict[str, Item] = {}
        for node in nodes:
            if not self.applies(node.step):
                items[node.key] = Item(node, NOT_NEEDED, Status(DONE, "not needed with these settings"))
            elif node.key in self.session.skipped:
                items[node.key] = Item(node, SKIPPED, Status(DONE, "skipped"))
            elif unmet := tuple(k for k in self.needs_of(node, nodes) if not items[k].satisfied):
                items[node.key] = Item(node, BLOCKED, Status("missing", "waiting for " + ", ".join(unmet)), unmet)
            else:
                status = await self.detect(node)
                items[node.key] = Item(node, DONE if status.done else READY, status)
        return list(items.values())

    @staticmethod
    def next_item(items: list[Item]) -> Item | None:
        return next((i for i in items if i.state == READY), None)

    @staticmethod
    def complete(items: list[Item]) -> bool:
        return all(i.satisfied for i in items)

    def checklist(self, items: list[Item]) -> str:
        nxt = self.next_item(items)
        width = max((len(i.node.key) for i in items), default=0)
        marks = {DONE: "x", READY: " ", BLOCKED: "-", SKIPPED: "s", NOT_NEEDED: "~"}
        lines = []
        for item in items:
            mark = ">" if item is nxt else marks[item.state]
            detail = "" if item.state in (DONE, NOT_NEEDED) else item.status.detail
            lines.append(
                f"[{mark}] {item.node.key.ljust(width)}  {self.title(item.node)}" + (f"  ({detail})" if detail else "")
            )
        return "\n".join(lines)

    # Explaining, confirming, running

    def describe(self, node: Node, with_title: bool = True) -> str:
        step, values = node.step, self.values(node)

        def fill(text: str) -> str:
            try:
                return render_text(text, values)
            except MissingValue:
                return text

        lines = [self.title(node)] if with_title else []
        if step.why:
            lines.append(fill(step.why))
        if step.inputs:
            shown = []
            for name in step.inputs:
                spec, _ = self.spec_for(node, name)
                if spec.secret:
                    shown.append(f"{name} = {'set' if self.env.secrets.has(name) else 'not set'} (hidden)")
                else:
                    shown.append(f"{name} = {values.get(name) or '(not set)'}")
            lines.append("Inputs: " + "; ".join(shown))
        if step.collect:
            lines.append(f"Add one with: add_target {step.collect}")
        if step.manual:
            lines.append("What you do by hand:\n" + textwrap.indent(fill(step.manual), "  "))
        if step.do:
            lines.append("There is an automated action: it shows exactly what it will do and waits for your yes.")
        if step.ack:
            lines.append("Nothing can detect this step: confirm it yourself when it is done.")
        if step.optional:
            lines.append("Optional: you may skip it.")
        return "\n".join(lines)

    async def run(self, node: Node) -> RunOutcome:
        step, console = node.step, self.env.console
        if step.collect:
            target = await self.add_target(step.collect)
            return RunOutcome(True, True, f"added {step.collect} {target.name}", await self.detect(node, fresh=True))
        if step.do is None:
            return RunOutcome(
                False, False, "This step has no automated action. Follow its manual instructions, then detect."
            )
        item = next(i for i in await self.evaluate() if i.node.key == node.key)
        if item.state in (BLOCKED, NOT_NEEDED, SKIPPED):
            return RunOutcome(False, False, f"Not now: {item.state}. {item.status.detail}")
        if item.state == DONE:
            return RunOutcome(False, True, "Already done.", item.status)
        if needed := self.missing(node):
            return RunOutcome(False, False, "Inputs are still needed first: " + ", ".join(needed) + ". Ask for them.")
        self.fill_generated(node)
        action = self.registry.actions[step.do.name]
        ctx = self.context(node, interactive=True)
        try:
            args = render(step.do.args, ctx.values)
            preview = action.preview(ctx, **args) if action.preview else f"Run the {action.name} action."
            if inspect.isawaitable(preview):
                preview = await preview
        except MissingValue as e:
            return RunOutcome(False, False, f"Input {e.args[0]} is not set yet. Ask for it.")
        console.say(f"\n{self.title(node)}\nThis will:\n{textwrap.indent(self.env.secrets.redact(preview), '  ')}")
        if not await console.confirm("Go ahead?"):
            self.session.record("declined", step=node.key)
            return RunOutcome(False, False, "The operator declined. Nothing was changed.")
        try:
            result = await action.run(ctx, **args)
        except Quit:
            raise
        except Exception as e:  # noqa: BLE001  report it; the step simply stays not done
            result = ActionResult(False, f"{type(e).__name__}: {e}")
        summary = self.env.secrets.redact(result.summary)
        self.session.record("ran", step=node.key, ok=str(result.ok), summary=summary[:500])
        self._cache.clear()
        return RunOutcome(True, result.ok, summary, await self.detect(node, fresh=True))

    async def acknowledge(self, node: Node) -> str:
        if not node.step.ack:
            raise EngineError("This step is confirmed by detection, not by you. Use detect.")
        if not await self.env.console.confirm(f"Have you completed: {self.title(node)}?"):
            return "The operator says it is not done yet."
        self.session.acks.add(node.key)
        self.session.record("acknowledged", step=node.key)
        self._cache.clear()
        return "recorded"

    async def skip(self, node: Node) -> str:
        if not node.step.optional:
            raise EngineError("This step is required.")
        if not await self.env.console.confirm(f"Skip: {self.title(node)}?"):
            return "The operator chose not to skip it."
        self.session.skipped.add(node.key)
        self.session.record("skipped", step=node.key)
        self._cache.clear()
        return "skipped"

    def finish_text(self) -> str:
        try:
            return render_text(self.pb.finish, self.values())
        except MissingValue:
            return self.pb.finish
