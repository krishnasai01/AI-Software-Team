"""Entry point inside a sandbox: `python -m agents.runner`.

Reads /task/task.json and /task/repo.bundle, runs one agent session, and writes /out/result.json
(plus /out/result.bundle with any new commits). It holds no GitHub credentials and pushes nothing:
the worker treats everything in /out as untrusted and verifies it before acting on it.
"""

import asyncio
import importlib
import json
import subprocess
import sys
import traceback
from pathlib import Path

import yaml

from agents._shared.guards import WORKSPACE
from core.model import AgentSession, run_agent

AGENTS_DIR = Path(__file__).parent
TASK_DIR = Path("/task")
OUT_DIR = Path("/out")
RESULT_REF = "refs/heads/agent-result"


def git(*args: str, cwd: str | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=cwd or WORKSPACE, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {p.stderr.strip()[-500:]}")
    return p.stdout.strip()


def load_role(role: str) -> tuple[dict, str]:
    config = yaml.safe_load((AGENTS_DIR / role / "config.yaml").read_text())
    prompt = (AGENTS_DIR / role / "prompt.md").read_text()
    return config, prompt


def untrusted(tag: str, text: str) -> str:
    return f'<{tag} trust="untrusted">\n{text}\n</{tag}>'


def build_prompt(task: dict) -> str:
    t, ctx, role, mode = task["ticket"], task.get("context") or {}, task["role"], task["mode"]
    lines = [
        f"Repository: {task['repo']}. Base branch: `base`. Mode: **{mode}**.",
        f"Ticket #{t['number']}: {t['title']}",
        untrusted("ticket", t.get("body") or "(no description)"),
    ]
    if task.get("memories"):
        lines.append("Notes from earlier work on this product (decisions and answers):")
        lines.append(untrusted("memories", "\n".join(f"- {m}" for m in task["memories"])))
    if role == "engineer":
        lines.append("You are on branch `work`. Commit your work there.")
        if ctx.get("guidance"):
            lines.append("Guidance from the tech lead or the owner:")
            lines.append(untrusted("guidance", ctx["guidance"]))
        if ctx.get("feedback"):
            lines.append("Review feedback to address:")
            lines.append(untrusted("review_feedback", ctx["feedback"]))
        if ctx.get("ci_failures"):
            lines.append("Failing CI checks: " + ", ".join(ctx["ci_failures"]))
    elif role == "reviewer":
        pr = task["pr"]
        lines.append(f"Review PR #{pr['number']}: the change is `git diff base...HEAD`.")
        lines.append(untrusted("pr_description", pr.get("body") or ""))
        lines.append(f"CI status: {ctx.get('ci', 'unknown')}")
    else:  # techlead
        for key in ("reason", "questions", "review_history", "engineer_summary"):
            if ctx.get(key):
                value = ctx[key] if isinstance(ctx[key], str) else json.dumps(ctx[key], indent=2)
                lines.append(f"{key.replace('_', ' ').capitalize()}:")
                lines.append(untrusted(key, value))
    lines.append("Finish by returning the structured result.")
    return "\n\n".join(lines)


def prepare_workspace(task: dict) -> str:
    """Clone from the bundle and check out `work`. Returns the starting commit."""
    Path(WORKSPACE).mkdir(parents=True, exist_ok=True)
    git("clone", "--quiet", "--no-tags", str(TASK_DIR / "repo.bundle"), ".")
    git("checkout", "--quiet", "-B", "work", "origin/work")
    git("branch", "--quiet", "-f", "base", "origin/base")
    git("remote", "remove", "origin")  # nothing to fetch from or push to
    author = task.get("git_author") or {}
    git("config", "user.name", author.get("name", f"agent-company {task['role']}"))
    git("config", "user.email", author.get("email", "agents@localhost"))
    return git("rev-parse", "HEAD")


def export_commits(start_sha: str) -> dict:
    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    if head == start_sha:
        return {"committed": False, "head_sha": head, "dirty": dirty}
    git("update-ref", RESULT_REF, head)
    git("bundle", "create", "--quiet", str(OUT_DIR / "result.bundle"), RESULT_REF, f"^{start_sha}")
    return {"committed": True, "head_sha": head, "dirty": dirty}


async def main(task: dict) -> dict:
    role = task["role"]
    config, role_prompt = load_role(role)
    hooks = importlib.import_module(f"agents.{role}.hooks")
    start_sha = prepare_workspace(task)
    outcome = await run_agent(
        AgentSession(
            prompt=build_prompt(task),
            instructions=role_prompt + "\n\n" + (task.get("instructions") or ""),
            model=config["model"],
            cwd=WORKSPACE,
            allowed_tools=config["tools"],
            max_turns=config["max_turns"],
            max_budget_usd=min(config["max_budget_usd_per_run"], task.get("budget_usd", 1e9)),
            output_schema=config.get("output_schema"),
            pre_tool_use=hooks.pre_tool_use,
            env={
                "ANTHROPIC_BASE_URL": task["model"]["base_url"],
                "ANTHROPIC_AUTH_TOKEN": task["model"]["api_key"],
                "ANTHROPIC_CUSTOM_HEADERS": f"langfuse_trace_id: {task['run_id']}",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
            },
        )
    )
    result = {
        "run_id": task["run_id"],
        "subtype": outcome.subtype,
        "ok": outcome.ok,
        "output": outcome.structured if isinstance(outcome.structured, dict) else None,
        "text": (outcome.text or "")[:4000],
        "turns": outcome.turns,
        "cost_usd": outcome.cost_usd,
        "tokens_in": outcome.tokens_in,
        "tokens_out": outcome.tokens_out,
        "errors": outcome.errors,
        "denials": outcome.denials,
        "start_sha": start_sha,
    }
    if role == "engineer":
        result.update(export_commits(start_sha))
    return result


def cli() -> None:
    task = json.loads((TASK_DIR / "task.json").read_text())
    try:
        result = asyncio.run(main(task))
    except Exception as e:  # noqa: BLE001 - the worker must always get a result
        traceback.print_exc(file=sys.stderr)
        result = {
            "run_id": task.get("run_id"),
            "subtype": "runner_error",
            "ok": False,
            "errors": [f"{type(e).__name__}: {e}"],
        }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "result.json").write_text(json.dumps(result, default=str))


if __name__ == "__main__":
    cli()
