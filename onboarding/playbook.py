"""A playbook is a team's onboarding as data: inputs to collect, steps to finish, targets to repeat them for.

The loader checks everything it can without running anything, so a typo in a playbook fails at load
time with a list of problems, not halfway through someone's onboarding.
"""

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

STEP_ID = re.compile(r"^[a-z][a-z0-9-]*$")
TARGET_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
INPUT_KINDS = ("text", "secret", "choice")


class PlaybookError(Exception):
    pass


class MissingValue(KeyError):
    """A template uses an input that has no value (yet)."""


@dataclass(frozen=True)
class InputSpec:
    name: str
    prompt: str
    kind: str = "text"  # text | secret | choice
    help: str = ""
    default: str | None = None
    options: tuple[str, ...] = ()
    pattern: str | None = None
    optional: bool = False
    persist: bool = True  # secrets: False keeps the value in memory for this run only
    generate: bool = False  # secrets: the engine makes a random token instead of asking
    suggest: tuple[dict, ...] = ()  # where to look first: {env: NAME} or {cmd: [argv]}

    @property
    def secret(self) -> bool:
        return self.kind == "secret"

    def problem(self, value: str) -> str | None:
        """Why `value` is not acceptable, or None."""
        if self.kind == "choice" and value not in self.options:
            return f"choose one of: {', '.join(self.options)}"
        if self.pattern and not re.fullmatch(self.pattern, value):
            return f"that doesn't look right. {self.help}".strip()
        return None


@dataclass(frozen=True)
class TargetSpec:
    """Something the checklist is repeated for, such as each product repo."""

    kind: str
    title: str
    inputs: dict[str, InputSpec]
    min: int = 0
    max: int | None = None
    default_name: str | None = None


@dataclass(frozen=True)
class Target:
    kind: str
    name: str
    values: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Call:
    """A reference to a probe or an action, with its arguments (templates)."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepSpec:
    id: str
    title: str
    why: str = ""
    needs: tuple[str, ...] = ()
    per: tuple[str, ...] = ()  # repeat for every target of these kinds
    collect: str | None = None  # done once `min` targets of this kind exist; running it adds one
    when: Mapping[str, str] = field(default_factory=dict)  # only applies if these inputs have these values
    inputs: tuple[str, ...] = ()  # asked before the step runs
    detect: tuple[Call, ...] = ()  # the step is done when every probe says so
    do: Call | None = None  # the automated action; the operator confirms it first
    manual: str = ""  # what the operator does by hand
    ack: bool = False  # nothing can detect it: the operator confirms it is done
    optional: bool = False  # the operator may skip it


@dataclass(frozen=True)
class Playbook:
    name: str
    title: str
    intro: str
    guide: str  # extra advice for the model
    finish: str  # shown when everything is done
    inputs: dict[str, InputSpec]
    targets: dict[str, TargetSpec]
    steps: tuple[StepSpec, ...]
    path: Path

    def step(self, step_id: str) -> StepSpec:
        return next(s for s in self.steps if s.id == step_id)


# Templates: {name} is replaced, {{ and }} are literal braces.


def placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field is not None}


def render_text(text: str, values: Mapping[str, str]) -> str:
    out = []
    for literal, field_name, _, _ in string.Formatter().parse(text):
        out.append(literal)
        if field_name is not None:
            if field_name not in values:
                raise MissingValue(field_name)
            out.append(str(values[field_name]))
    return "".join(out)


def render(value: Any, values: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return render_text(value, values)
    if isinstance(value, list):
        return [render(v, values) for v in value]
    if isinstance(value, dict):
        return {k: render(v, values) for k, v in value.items()}
    return value


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


# Loading


def load_playbook(directory: Path) -> Playbook:
    path = directory / "playbook.yaml"
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise PlaybookError(f"{path}: {e}") from e
    if not isinstance(data, dict):
        raise PlaybookError(f"{path}: expected a mapping at the top level")
    problems: list[str] = []
    playbook = _build(data, directory, problems)
    if problems:
        raise PlaybookError(f"{path}:\n" + "\n".join(f"- {p}" for p in problems))
    return playbook


def check_refs(playbook: Playbook, probes: Mapping[str, Any], actions: Mapping[str, Any]) -> None:
    """Every probe and action a step names must be registered."""
    problems = []
    for step in playbook.steps:
        for call in step.detect:
            if call.name not in probes:
                problems.append(f"step {step.id}: unknown probe {call.name!r}")
        if step.do and step.do.name not in actions:
            problems.append(f"step {step.id}: unknown action {step.do.name!r}")
    if problems:
        raise PlaybookError(f"{playbook.path}:\n" + "\n".join(f"- {p}" for p in problems))


def _only(d: dict, allowed: set[str], where: str, problems: list[str]) -> None:
    for key in sorted(set(d) - allowed):
        problems.append(f"{where}: unknown field {key!r}")


def _input(name: str, raw: Any, where: str, problems: list[str]) -> InputSpec:
    if not isinstance(raw, dict) or "prompt" not in raw:
        problems.append(f"{where}: needs a mapping with a prompt")
        return InputSpec(name, name)
    _only(
        raw,
        {"prompt", "kind", "help", "default", "options", "pattern", "optional", "persist", "generate", "suggest"},
        where,
        problems,
    )
    spec = InputSpec(
        name=name,
        prompt=str(raw["prompt"]),
        kind=raw.get("kind", "text"),
        help=str(raw.get("help", "")).strip(),
        default=None if raw.get("default") is None else str(raw["default"]),
        options=tuple(str(o) for o in raw.get("options", ())),
        pattern=raw.get("pattern"),
        optional=bool(raw.get("optional", False)),
        persist=bool(raw.get("persist", True)),
        generate=bool(raw.get("generate", False)),
        suggest=tuple(raw.get("suggest", ())),
    )
    if spec.kind not in INPUT_KINDS:
        problems.append(f"{where}: kind must be one of {', '.join(INPUT_KINDS)}")
    if spec.kind == "choice" and not spec.options:
        problems.append(f"{where}: a choice needs options")
    if spec.default is not None and spec.kind == "choice" and spec.default not in spec.options:
        problems.append(f"{where}: the default is not one of the options")
    if spec.generate and not spec.secret:
        problems.append(f"{where}: only secrets can be generated")
    if spec.secret and spec.default is not None:
        problems.append(f"{where}: a secret cannot have a default")
    if spec.pattern:
        try:
            re.compile(spec.pattern)
        except re.error as e:
            problems.append(f"{where}: bad pattern: {e}")
    return spec


def _call(raw: Any, key: str, where: str, problems: list[str]) -> Call:
    if not isinstance(raw, dict) or key not in raw:
        problems.append(f"{where}: expected a mapping with `{key}:`")
        return Call("?")
    _only(raw, {key, "args"}, where, problems)
    return Call(str(raw[key]), dict(raw.get("args") or {}))


def _build(data: dict, directory: Path, problems: list[str]) -> Playbook:
    _only(data, {"name", "title", "intro", "guide", "finish", "inputs", "targets", "steps"}, "playbook", problems)
    inputs = {n: _input(n, r, f"input {n}", problems) for n, r in (data.get("inputs") or {}).items()}
    targets: dict[str, TargetSpec] = {}
    for kind, raw in (data.get("targets") or {}).items():
        where = f"target {kind}"
        _only(raw, {"title", "inputs", "min", "max", "default_name"}, where, problems)
        t_inputs = {n: _input(n, r, f"{where} input {n}", problems) for n, r in (raw.get("inputs") or {}).items()}
        for n, spec in t_inputs.items():
            if spec.secret:
                problems.append(f"{where} input {n}: targets cannot hold secrets")
        targets[kind] = TargetSpec(
            kind, raw.get("title", kind), t_inputs, int(raw.get("min", 0)), raw.get("max"), raw.get("default_name")
        )

    steps: list[StepSpec] = []
    seen: set[str] = set()
    for raw in data.get("steps") or []:
        where = f"step {raw.get('id', '?')}"
        _only(
            raw,
            {
                "id",
                "title",
                "why",
                "needs",
                "per",
                "collect",
                "when",
                "inputs",
                "detect",
                "do",
                "manual",
                "ack",
                "optional",
            },
            where,
            problems,
        )
        per = raw.get("per") or ()
        step = StepSpec(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", raw.get("id", ""))),
            why=str(raw.get("why", "")).strip(),
            needs=tuple(raw.get("needs") or ()),
            per=(per,) if isinstance(per, str) else tuple(per),
            collect=raw.get("collect"),
            when={k: str(v) for k, v in (raw.get("when") or {}).items()},
            inputs=tuple(raw.get("inputs") or ()),
            detect=tuple(_call(c, "probe", f"{where} detect", problems) for c in raw.get("detect") or ()),
            do=_call(raw["do"], "action", f"{where} do", problems) if raw.get("do") else None,
            manual=str(raw.get("manual", "")).strip(),
            ack=bool(raw.get("ack", False)),
            optional=bool(raw.get("optional", False)),
        )
        _check_step(step, seen, inputs, targets, problems)
        steps.append(step)
        seen.add(step.id)

    for field_name in ("intro", "finish"):
        for name in _bad_names(str(data.get(field_name, "")), inputs):
            problems.append(f"{field_name}: {{{name}}} is not a plain team input")

    return Playbook(
        name=str(data.get("name", directory.name)),
        title=str(data.get("title", data.get("name", directory.name))),
        intro=str(data.get("intro", "")).strip(),
        guide=str(data.get("guide", "")).strip(),
        finish=str(data.get("finish", "")).strip(),
        inputs=inputs,
        targets=targets,
        steps=tuple(steps),
        path=directory / "playbook.yaml",
    )


def _bad_names(text: str, inputs: dict[str, InputSpec]) -> list[str]:
    try:
        names = placeholders(text)
    except ValueError:
        return [text[:20]]
    return sorted(n for n in names if n not in inputs or inputs[n].secret)


def _check_step(
    step: StepSpec,
    seen: set[str],
    inputs: dict[str, InputSpec],
    targets: dict[str, TargetSpec],
    problems: list[str],
) -> None:
    where = f"step {step.id}"
    if not STEP_ID.match(step.id):
        problems.append(f"{where}: ids are lowercase words joined by dashes")
    if step.id in seen:
        problems.append(f"{where}: duplicate id")
    for need in step.needs:
        if need not in seen:
            problems.append(f"{where}: needs {need!r}, which must be an earlier step")
    if step.per and step.collect:
        problems.append(f"{where}: a step is either per a target or collects one, not both")
    for kind in (*step.per, *([step.collect] if step.collect else [])):
        if kind not in targets:
            problems.append(f"{where}: unknown target kind {kind!r}")
    if step.collect and (step.do or step.detect):
        problems.append(f"{where}: a collect step has no probes or action of its own")
    if not (step.do or step.manual or step.detect or step.inputs or step.ack or step.collect):
        problems.append(f"{where}: does nothing; give it inputs, probes, an action or manual steps")
    for key in step.when:
        if key not in inputs or inputs[key].secret:
            problems.append(f"{where}: `when` names {key!r}, which is not a plain team input")

    kinds = [targets[k] for k in step.per if k in targets]
    shared = set.intersection(*(set(t.inputs) for t in kinds)) if kinds else set()
    for name in step.inputs:
        if name not in shared and name not in inputs:
            problems.append(f"{where}: input {name!r} is not defined")

    allowed = {"target", "kind", *step.inputs, *shared}
    texts = [
        step.title,
        step.why,
        step.manual,
        *_strings([c.args for c in step.detect]),
        *_strings(step.do.args if step.do else {}),
    ]
    for text in texts:
        try:
            names = placeholders(text)
        except ValueError as e:
            problems.append(f"{where}: bad template {text[:40]!r}: {e} (write {{{{ and }}}} for literal braces)")
            continue
        for name in sorted(names - allowed):
            problems.append(f"{where}: {{{name}}} is not available here; add it to the step's inputs")
        for name in names & allowed:
            spec = inputs.get(name) or next((t.inputs[name] for t in kinds if name in t.inputs), None)
            if spec and spec.secret:
                problems.append(f"{where}: {{{name}}} is a secret and cannot be used in a template")
