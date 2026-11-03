"""python -m onboarding [playbook]: the onboarding assistant. See docs/onboarding.md."""

import argparse
import asyncio
import sys
from pathlib import Path

from onboarding import packs
from onboarding.chat import DEFAULT_MODEL, ModelUnavailable, run_chat, run_wizard
from onboarding.console import Console, Quit, TerminalConsole
from onboarding.engine import Onboarding
from onboarding.playbook import PlaybookError
from onboarding.registry import Env
from onboarding.secrets import SecretStore
from onboarding.session import Session, SessionError, default_dir

ROOT = Path(__file__).resolve().parents[1]  # this repo: templates and infra files that playbooks use


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m onboarding", description="Interactive onboarding assistant.")
    p.add_argument("playbook", nargs="?", help="a bundled playbook's name, or the path of a playbook directory")
    p.add_argument("--list", action="store_true", help="list the bundled playbooks")
    p.add_argument("--status", action="store_true", help="check what is done, print it, and exit")
    p.add_argument("--no-model", action="store_true", help="a step-by-step wizard: no model, no API key")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"model for the chat (default {DEFAULT_MODEL})")
    p.add_argument("--budget", type=float, default=2.0, help="model spend cap for one session, in USD")
    p.add_argument("--state-dir", type=Path, help="where progress and stored secrets are kept")
    p.add_argument("--reset", action="store_true", help="forget this playbook's progress and stored secrets")
    return p


async def amain(args: argparse.Namespace, console: Console | None = None, env: Env | None = None) -> int:
    console = console or TerminalConsole()
    available = packs.bundled()
    if args.list:
        for name in available:
            console.say(name)
        return 0
    spec = args.playbook or (next(iter(available)) if len(available) == 1 else None)
    if spec is None:
        console.say("Name a playbook: " + ", ".join(available))
        return 2
    try:
        playbook, registry = packs.load(spec)
        state_dir = args.state_dir or default_dir(playbook.name)
        session = Session.load(state_dir / "session.json", playbook.name)
    except (PlaybookError, SessionError) as e:
        console.say(str(e))
        return 2

    if args.reset:
        if await console.confirm(f"Forget the progress and stored secrets of {playbook.name}?"):
            for name in ("session.json", "secrets.json"):
                (state_dir / name).unlink(missing_ok=True)
            console.say("Forgotten.")
        return 0

    env = env or Env(console, SecretStore(state_dir / "secrets.json"), ROOT)
    ob = Onboarding(playbook, registry, session, env)
    if args.status:
        console.say("Checking what is done...")
        items = await ob.evaluate(fresh=True)
        console.say(ob.checklist(items))
        return 0 if ob.complete(items) else 1

    try:
        if args.no_model:
            complete = await run_wizard(ob)
        else:
            try:
                complete = await run_chat(ob, args.model, args.budget)
            except ModelUnavailable as e:
                console.say(f"Could not start the model chat: {e}")
                console.say("Set ANTHROPIC_API_KEY (or log in to Claude Code), or use --no-model.")
                if not await console.confirm("Continue with the step-by-step wizard instead?", default=True):
                    return 1
                complete = await run_wizard(ob)
    except Quit:
        console.say("Stopped. Progress is saved; run it again to resume.")
        return 1
    return 0 if complete else 1


def main() -> int:
    try:
        return asyncio.run(amain(parser().parse_args()))
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run it again to resume.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
