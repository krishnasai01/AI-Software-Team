from pathlib import Path

import pytest

from onboarding import packs
from onboarding.console import ScriptedConsole
from onboarding.engine import Onboarding
from onboarding.playbook import (
    MissingValue,
    PlaybookError,
    check_refs,
    load_playbook,
    placeholders,
    render,
    render_text,
)
from onboarding.registry import Env, Registry
from onboarding.secrets import SecretStore
from onboarding.session import Session
from tests.onboarding_fakes import FakeShells

ROOT = Path(__file__).resolve().parents[1]

BASE = """
name: t
inputs:
  a: {prompt: A}
  s: {prompt: S, kind: secret}
targets:
  kind: {title: thing, inputs: {x: {prompt: X}}}
"""


def load(tmp_path, steps: str, head: str = BASE):
    (tmp_path / "playbook.yaml").write_text(head + "steps:\n" + steps)
    return load_playbook(tmp_path)


def problems(tmp_path, steps: str, head: str = BASE) -> str:
    with pytest.raises(PlaybookError) as e:
        load(tmp_path, steps, head)
    return str(e.value)


def test_a_valid_playbook_loads(tmp_path):
    pb = load(tmp_path, "  - {id: one, title: 'One {a}', inputs: [a], manual: 'do {a}'}\n")
    assert [s.id for s in pb.steps] == ["one"]
    assert pb.step("one").inputs == ("a",)


@pytest.mark.parametrize(
    "steps, expected",
    [
        ("  - {id: one, title: One, inputs: [a]}\n  - {id: one, title: Again, inputs: [a]}\n", "duplicate id"),
        (
            "  - {id: one, title: One, needs: [two], inputs: [a]}\n  - {id: two, title: Two, inputs: [a]}\n",
            "must be an earlier step",
        ),
        ("  - {id: one, title: One, inputs: [nope]}\n", "input 'nope' is not defined"),
        ("  - {id: one, title: One, inputs: [a], manual: 'use {b}'}\n", "{b} is not available here"),
        ("  - {id: one, title: One, inputs: [a, s], manual: 'key {s}'}\n", "{s} is a secret"),
        ("  - {id: one, title: One, inputs: [a], per: [nope]}\n", "unknown target kind 'nope'"),
        ("  - {id: one, title: One, inputs: [a], per: [kind], collect: kind}\n", "not both"),
        ("  - {id: one, title: One}\n", "does nothing"),
        ("  - {id: One, title: One, inputs: [a]}\n", "lowercase words"),
        ("  - {id: one, title: One, inputs: [a], bogus: 1}\n", "unknown field 'bogus'"),
        ("  - {id: one, title: One, inputs: [a], when: {s: x}}\n", "not a plain team input"),
        ("  - {id: one, title: One, inputs: [a], manual: 'a { b'}\n", "bad template"),
        ("  - {id: one, title: One, inputs: [a], detect: [{args: {}}]}\n", "expected a mapping with `probe:`"),
        ("  - {id: one, title: One, collect: kind, inputs: [a], detect: [{probe: p}]}\n", "no probes or action"),
        ("  - {id: one, title: One, per: [kind], inputs: [x, a], manual: '{x} {target} {kind}'}\n", None),
    ],
)
def test_mistakes_are_caught_when_the_playbook_loads(tmp_path, steps, expected):
    if expected is None:
        load(tmp_path, steps)  # control: per-step inputs, target and kind names are fine
    else:
        assert expected in problems(tmp_path, steps)


@pytest.mark.parametrize(
    "head, expected",
    [
        ("inputs: {a: {prompt: A, kind: choice}}\n", "a choice needs options"),
        ("inputs: {a: {prompt: A, kind: choice, options: [x], default: y}}\n", "not one of the options"),
        ("inputs: {a: {prompt: A, generate: true}}\n", "only secrets can be generated"),
        ("inputs: {a: {prompt: A, kind: secret, default: x}}\n", "cannot have a default"),
        ("inputs: {a: {prompt: A, pattern: '('}}\n", "bad pattern"),
        ("inputs: {a: {prompt: A, kind: nope}}\n", "kind must be one of"),
        ("inputs: {a: {prompt: A, wat: 1}}\n", "unknown field 'wat'"),
        ("inputs: {a: {help: no prompt}}\n", "needs a mapping with a prompt"),
        ("targets: {k: {inputs: {x: {prompt: X, kind: secret}}}}\n", "targets cannot hold secrets"),
        ("inputs: {a: {prompt: A}}\nfinish: 'done {zzz}'\n", "finish: {zzz} is not a plain team input"),
    ],
)
def test_input_and_target_mistakes(tmp_path, head, expected):
    assert expected in problems(tmp_path, "  - {id: one, title: One, manual: hi}\n", head=f"name: t\n{head}")


def test_unknown_probes_and_actions_are_caught_against_the_registry(tmp_path):
    pb = load(tmp_path, "  - {id: one, title: One, inputs: [a], detect: [{probe: nope}], do: {action: nada}}\n")
    with pytest.raises(PlaybookError) as e:
        check_refs(pb, {}, {})
    assert "unknown probe 'nope'" in str(e.value) and "unknown action 'nada'" in str(e.value)


def test_a_missing_or_malformed_file_is_a_playbook_error(tmp_path):
    with pytest.raises(PlaybookError):
        load_playbook(tmp_path)
    (tmp_path / "playbook.yaml").write_text("- just\n- a list\n")
    with pytest.raises(PlaybookError, match="mapping"):
        load_playbook(tmp_path)


def test_templates():
    assert placeholders("a {x} b {y} {{literal}}") == {"x", "y"}
    assert render_text("run {cmd} {{now}}", {"cmd": "ls"}) == "run ls {now}"
    with pytest.raises(MissingValue):
        render_text("{nope}", {})
    assert render({"a": ["{x}", 3], "b": {"c": "{x}!"}}, {"x": "1"}) == {"a": ["1", 3], "b": {"c": "1!"}}


# The bundled playbook


def test_bundled_playbooks_load_with_every_probe_and_action_registered():
    assert "software-team" in packs.bundled()
    for name in packs.bundled():
        playbook, registry = packs.load(name)
        assert playbook.steps and isinstance(registry, Registry)


def test_loading_an_unknown_playbook_lists_the_bundled_ones():
    with pytest.raises(PlaybookError, match="Bundled: .*software-team"):
        packs.load("nope")


def full_software_team() -> Onboarding:
    """The software-team playbook with every input answered, and a scratch and a product target."""
    playbook, registry = packs.load("software-team")
    session = Session(None, playbook.name)
    for name, spec in playbook.inputs.items():
        if not spec.secret:
            session.answers[name] = spec.default or "x"
    rehearsal_image = "agent-company/sandbox-rehearsal:latest"
    session.targets = {
        "scratch": {"scratch": {"repo": "acme/scratch", "image": rehearsal_image, "ci_checks": ""}},
        "product": {"shop": {"repo": "acme/shop", "image": "agent-company/sandbox-python:latest", "ci_checks": "test"}},
    }
    env = Env(ScriptedConsole(), SecretStore(None), ROOT, shells=FakeShells())
    return Onboarding(playbook, registry, session, env)


def test_every_template_in_the_software_team_playbook_renders():
    ob = full_software_team()
    keys = {n.key for n in ob.nodes()}
    assert keys >= {"repo-ruleset@scratch", "repo-ruleset@shop", "rehearsal@scratch", "gvisor"}
    assert "rehearsal@shop" not in keys  # the rehearsal is for the scratch repo only
    for node in ob.nodes():
        values = ob.values(node)
        assert ob.describe(node)
        for call in [*node.step.detect, *([node.step.do] if node.step.do else [])]:
            render(call.args, values)  # raises MissingValue on any placeholder that has no value
        assert "{" not in ob.title(node)
    assert ob.finish_text()


def test_the_software_team_order_makes_sense():
    ob = full_software_team()
    keys = [n.key for n in ob.nodes()]
    before = [
        ("servers", "tools"),
        ("tools", "age-keys"),
        ("checkout", "age-keys"),
        ("webhook", "secrets"),
        ("age-keys", "secrets"),
        ("secrets", "stacks"),
        ("images", "stacks"),
        ("stacks", "database"),
        ("database", "scratch-repo"),
        ("repo-brain@scratch", "repo-ruleset@scratch"),
    ]
    for early, late in before:
        assert keys.index(early) < keys.index(late), (early, late)
    # The brain PR must merge before branch protection exists, or protection would block the operator's own merge.
    assert "repo-brain" in ob.pb.step("repo-ruleset").needs
    # Nothing for a real product can start before the rehearsal is done.
    assert set(ob.pb.step("product-repo").needs) >= {"database", "rehearsal", "rehearsal-safety"}
