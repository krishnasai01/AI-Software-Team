"""Fakes for the onboarding tests: scripted shells, a GitHub that can be set up, an App's own identity."""

from dataclasses import dataclass

from onboarding.console import ScriptedConsole
from onboarding.engine import Onboarding
from onboarding.playbook import check_refs, load_playbook
from onboarding.registry import ActionResult, Env, Registry, done, missing
from onboarding.secrets import SecretStore
from onboarding.session import Session
from onboarding.shell import Result
from tests.fakes import FakeGitHub


@dataclass
class Call:
    host: str
    argv: list[str]
    stdin: str | None


class FakeShell:
    def __init__(self, host: str, owner: "FakeShells"):
        self.host = host
        self.owner = owner

    @property
    def local(self) -> bool:
        return self.host == "local"

    async def run(self, argv, *, stdin=None, timeout=120, on_line=None) -> Result:
        self.owner.calls.append(Call(self.host, list(argv), stdin))
        text = " ".join(argv)
        for needle, reply in self.owner.rules:
            if needle in text:
                return reply(argv, stdin) if callable(reply) else reply
        return Result(0, "", "")

    async def script(self, script: str, **kw) -> Result:
        return await self.run(["bash", "-c", script], **kw)


class FakeShells:
    """A shell factory that records every command and answers from rules (first match wins)."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.rules: list[tuple[str, Result | object]] = []

    def __call__(self, host: str) -> FakeShell:
        return FakeShell(host, self)

    def when(self, needle: str, out: str = "", err: str = "", code: int = 0) -> None:
        self.rules.append((needle, Result(code, out, err)))

    def reply(self, needle: str, fn) -> None:
        self.rules.append((needle, fn))

    def ran(self, needle: str) -> list[Call]:
        return [c for c in self.calls if needle in " ".join(c.argv)]


class FakeRepoGitHub(FakeGitHub):
    """FakeGitHub plus what onboarding sets up: settings, rulesets, files, issues without a label."""

    def __init__(self, repo: str = "acme/shop"):
        super().__init__(repo)
        self.settings = {
            "default_branch": "main",
            "allow_auto_merge": False,
            "delete_branch_on_merge": False,
            "permissions": {"admin": True},
        }
        self.rulesets: dict[int, dict] = {}
        self.commits: list[tuple[str, dict[str, str]]] = []
        self.check_runs: dict[tuple[str, str], dict] = {}

    async def get_repo(self, repo):
        return dict(self.settings)

    async def update_repo(self, repo, **fields):
        self.settings.update(fields)

    async def list_rulesets(self, repo):
        return [{"id": i, "name": p["name"]} for i, p in self.rulesets.items()]

    async def create_ruleset(self, repo, payload):
        rid = len(self.rulesets) + 1
        self.rulesets[rid] = payload
        return rid

    async def update_ruleset(self, repo, ruleset_id, payload):
        self.rulesets[ruleset_id] = payload

    async def branch_rules(self, repo, branch):
        return [r for p in self.rulesets.values() for r in p["rules"]]

    async def put_files(self, repo, base, branch, files, message):
        self.commits.append((branch, dict(files)))
        return f"commit-{len(self.commits)}"

    async def list_issues(self, repo, label=None, state="all"):
        return [
            i
            for i in self.issues.values()
            if (state == "all" or i["state"] == state) and (label is None or label in {x["name"] for x in i["labels"]})
        ]

    async def list_prs(self, repo, state="open", **params):
        prs = [p for p in self.prs.values() if state == "all" or p["state"] == state]
        if "head" in params:
            prs = [p for p in prs if f"{repo.split('/')[0]}:{p['head']['ref']}" == params["head"]]
        return prs

    async def check_run(self, repo, sha, name):
        return self.check_runs.get((sha, name))


class FakeAdmin:
    """An App as itself: what GitHub says about it."""

    def __init__(self, slug="team-app", permissions=None, events=(), hook=None, installed=()):
        self._info = {"slug": slug, "permissions": permissions or {}, "events": list(events)}
        self.hook = hook or {}
        self.installed = set(installed)
        self.secret = None

    async def info(self):
        return self._info

    async def hook_config(self):
        return self.hook

    async def set_hook_config(self, url, secret):
        self.hook = {"url": url, "content_type": "json"}
        self.secret = secret

    async def installed_on(self, repo):
        return repo in self.installed


TOY = """
name: toy
title: Toy team
intro: Hello
finish: Done, {who}
guide: Be kind.
inputs:
  who: {prompt: Your name, pattern: '[a-z]+'}
  nick: {prompt: Nickname, optional: true}
  color: {prompt: Colour, kind: choice, options: [red, blue], default: red}
  mode: {prompt: Mode, kind: choice, options: [fast, slow], default: fast}
  key: {prompt: API key, kind: secret, persist: false}
  auto: {prompt: Generated, kind: secret, generate: true}
targets:
  site:
    title: site
    min: 1
    max: 2
    inputs:
      url: {prompt: URL, pattern: 'https://[a-z.]+'}
steps:
  - id: base
    title: Base for {who}
    inputs: [who, nick, color]
    detect: [{probe: flag, args: {name: base}}]
    do: {action: raise_flag, args: {name: base}}
  - id: net
    title: Network
    needs: [base]
    inputs: [key, auto]
    detect: [{probe: flag, args: {name: net}}]
    do: {action: raise_flag, args: {name: net}}
  - id: fast-only
    title: Only when fast
    when: {mode: fast}
    needs: [base]
    detect: [{probe: flag, args: {name: fast}}]
  - id: sites
    title: The sites
    needs: [net]
    collect: site
  - id: site-up
    title: Site {url}
    per: [site]
    needs: [sites]
    inputs: [url]
    detect: [{probe: flag, args: {name: 'up-{target}'}}]
    do: {action: raise_flag, args: {name: 'up-{target}'}}
  - id: checked
    title: Everything checked
    needs: [site-up]
    ack: true
  - id: extra
    title: Optional extra
    optional: true
    needs: [base]
    detect: [{probe: flag, args: {name: extra}}]
  - id: bad
    title: Always fails
    optional: true
    needs: [base]
    detect: [{probe: flag, args: {name: bad}}]
    do: {action: broken}
  - id: fragile
    title: Probe that crashes
    optional: true
    needs: [base]
    detect: [{probe: boom}]
  - id: keyed
    title: Probe that needs the key
    optional: true
    needs: [base]
    inputs: [key]
    detect: [{probe: needs_key}]
  - id: leak
    title: Leaks on purpose
    optional: true
    needs: [net]
    inputs: [key]
    detect: [{probe: leaky_probe}]
    do: {action: leaky}
"""


def toy_registry(flags: dict[str, bool]) -> Registry:
    reg = Registry()

    @reg.probe("flag")
    async def flag(ctx, name):
        return done(f"{name} is up") if flags.get(name) else missing(f"{name} is down")

    @reg.probe("boom")
    async def boom(ctx):
        raise ValueError("kaput")

    @reg.probe("needs_key")
    async def needs_key(ctx):
        await ctx.secret("key")
        return done("the key works")

    @reg.probe("leaky_probe")
    async def leaky_probe(ctx):
        return missing(f"key is {ctx.secrets.get('key')}")

    @reg.action("raise_flag", preview=lambda ctx, name: f"raise {name}")
    async def raise_flag(ctx, name):
        flags[name] = True
        return ActionResult(True, f"raised {name}")

    @reg.action("broken")
    async def broken(ctx):
        return ActionResult(False, "it broke")

    @reg.action("leaky")
    async def leaky(ctx):
        return ActionResult(True, f"used {ctx.secrets.get('key')}")

    return reg


class Toy:
    """An Onboarding over a throwaway playbook, with scripted answers and flag-based probes."""

    def __init__(self, tmp_path, answers=(), playbook: str = TOY, state=None):
        (tmp_path / "playbook.yaml").write_text(playbook)
        self.pb = load_playbook(tmp_path)
        self.flags: dict[str, bool] = {}
        self.registry = toy_registry(self.flags)
        check_refs(self.pb, self.registry.probes, self.registry.actions)
        self.console = ScriptedConsole(answers)
        self.shells = FakeShells()
        secrets_path = state / "secrets.json" if state else None
        self.secrets = SecretStore(secrets_path)
        self.env = Env(self.console, self.secrets, tmp_path, shells=self.shells)
        self.session = Session.load(state / "session.json" if state else None, self.pb.name)
        self.ob = Onboarding(self.pb, self.registry, self.session, self.env)

    def node(self, ref: str):
        return self.ob.resolve(ref)

    async def states(self, fresh: bool = True) -> dict[str, str]:
        return {i.node.key: i.state for i in await self.ob.evaluate(fresh)}
