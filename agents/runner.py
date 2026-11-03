"""Entry point inside a sandbox: `python -m agents.runner < task.json`.

Clones the repo, runs one agent session for the task's role, and (for the engineer) pushes the
branch if the final diff is clean of protected paths. Always prints exactly one
`AGENT_RESULT <json>` line, even on failure.

The GitHub token arrives on stdin, is held only in this process's memory, and reaches git through
`-c http.extraheader`, so it is never written to .git/config or to any environment the agent sees.
"""

import asyncio
import base64
import importlib
import json
import subprocess
import sys
import traceback
from pathlib import Path

import yaml

from agents._shared.guards import WORKSPACE, protected_paths_touched
from core.model import AgentSession, run_agent

AGENTS_DIR = Path(__file__).parent
RESULT_MARKER = "AGENT_RESULT "


class Git:
    def __init__(self, token: str, cwd: str = WORKSPACE):
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        self.auth = ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}"]
        self.cwd = cwd

    def __call__(self, *args: str, auth: bool = False, check: bool = True) -> str:
        cmd = ["git", *(self.auth if auth else []), *args]
        p = subprocess.run(cmd, cwd=self.cwd, capture_output=True, text=True)
        if check and p.returncode != 0:
            # Never echo the command: it may hold the auth header.
            raise RuntimeError(f"git {args[0]} failed: {p.stderr.strip()[-500:]}")
        return p.stdout.strip()


def load_role(role: str) -> tuple[dict, str]:
    config = yaml.safe_load((AGENTS_DIR / role / "config.yaml").read_text())
    prompt = (AGENTS_DIR / role / "prompt.md").read_text()
    return config, prompt


def product_brain(workspace: str = WORKSPACE) -> str:
    brain_dir = Path(workspace) / "docs" / "brain"
    if not brain_dir.is_dir():
        return ""
    parts = [f"## {p.name}\n\n{p.read_text()}" for p in sorted(brain_dir.glob("*.md"))]
    return "\n\n# Product brain\n\n" + "\n\n".join(parts)


def untrusted(tag: str, text: str) -> str:
    return f'<{tag} trust="untrusted">\n{text}\n</{tag}>'


def build_prompt(task: dict) -> str:
    t = task["ticket"]
    lines = [
        f"Repository: {task['repo']}  Base branch: {task['base_branch']}",
        f"Ticket #{t['number']}: {t['title']}",
        untrusted("ticket", t.get("body") or "(no description)"),
    ]
    if task["role"] == "engineer":
        lines.append(f"You are on branch `{task['branch']}`. Commit your work there; do not push.")
        if task.get("feedback"):
            lines.append("This is a rework round. The reviewer requested these changes:")
            lines.append(untrusted("review_feedback", task["feedback"]))
    else:
        pr = task["pr"]
        lines.append(f"Review PR #{pr['number']}: diff with `git diff origin/{task['base_branch']}...HEAD`.")
        lines.append(untrusted("pr_description", pr.get("body") or ""))
        lines.append(f"CI status: {task.get('ci_summary', 'unknown')}")
    lines.append("Finish by returning the structured result.")
    return "\n\n".join(lines)


def prepare_workspace(task: dict, git: Git) -> str:
    """Clone and check out the right ref. Returns the starting commit sha."""
    Path(WORKSPACE).mkdir(parents=True, exist_ok=True)
    git("clone", "--quiet", "--no-tags", f"https://github.com/{task['repo']}.git", ".", auth=True)
    author = task.get("git_author") or {}
    git("config", "user.name", author.get("name", f"agent-company {task['role']}"))
    git("config", "user.email", author.get("email", "agents@localhost"))
    if task["role"] == "engineer":
        branch = task["branch"]
        remote = git("ls-remote", "--heads", "origin", branch, auth=True)
        if remote:
            git("fetch", "--quiet", "origin", branch, auth=True)
            git("checkout", "--quiet", "-b", branch, f"origin/{branch}")
        else:
            git("checkout", "--quiet", "-b", branch, f"origin/{task['base_branch']}")
    else:
        number = task["pr"]["number"]
        git("fetch", "--quiet", "origin", f"pull/{number}/head:pr-{number}", auth=True)
        git("checkout", "--quiet", f"pr-{number}")
    return git("rev-parse", "HEAD")


def finish_engineer(task: dict, git: Git, start_sha: str, structured: dict | None) -> dict:
    head = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain")
    if head == start_sha:
        return {"pushed": False, "head_sha": head, "note": "no new commits", "dirty": bool(dirty)}
    changed = git("diff", "--name-only", f"origin/{task['base_branch']}...HEAD").splitlines()
    touched = protected_paths_touched(changed)
    if touched:
        return {
            "pushed": False,
            "head_sha": head,
            "blocked_paths": touched,
            "note": "diff touches protected paths; not pushed",
        }
    if structured and structured.get("status") == "blocked":
        return {"pushed": False, "head_sha": head, "note": "agent reported blocked; not pushed"}
    git("push", "--quiet", "origin", f"HEAD:refs/heads/{task['branch']}", auth=True)
    return {"pushed": True, "head_sha": head, "changed_files": changed, "dirty": bool(dirty)}


async def main(task: dict) -> dict:
    secrets = task.pop("secrets")
    git = Git(secrets["github_token"])
    role = task["role"]
    config, role_prompt = load_role(role)
    hooks = importlib.import_module(f"agents.{role}.hooks")

    start_sha = prepare_workspace(task, git)
    outcome = await run_agent(
        AgentSession(
            prompt=build_prompt(task),
            system_prompt=role_prompt + product_brain(),
            model=config["model"],
            cwd=WORKSPACE,
            allowed_tools=config["tools"],
            max_turns=config["max_turns"],
            max_budget_usd=min(config["max_budget_usd_per_run"], task.get("budget_remaining_usd", 1e9)),
            output_schema=config.get("output_schema"),
            pre_tool_use=hooks.pre_tool_use,
            mcp_servers=config.get("mcp_servers") or {},
            env={
                "ANTHROPIC_BASE_URL": secrets["model_base_url"],
                "ANTHROPIC_API_KEY": secrets["model_api_key"],
                "ANTHROPIC_CUSTOM_HEADERS": f"langfuse_trace_id: {task['run_id']}",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
            },
        )
    )
    result = {
        "run_id": task["run_id"],
        "outcome": "completed" if outcome.ok else outcome.subtype,
        "output": outcome.structured,
        "text": outcome.text,
        "turns": outcome.turns,
        "cost_usd": outcome.cost_usd,
        "tokens_in": outcome.tokens_in,
        "tokens_out": outcome.tokens_out,
        "errors": outcome.errors,
    }
    if role == "engineer":
        result["git"] = finish_engineer(task, git, start_sha, outcome.structured)
        if outcome.ok and isinstance(outcome.structured, dict) and outcome.structured.get("status") == "blocked":
            result["outcome"] = "blocked"
        elif result["git"].get("blocked_paths"):
            result["outcome"] = "blocked"
    return result


def cli() -> None:
    task = json.loads(sys.stdin.read())
    try:
        result = asyncio.run(main(task))
    except Exception as e:  # noqa: BLE001 - the worker must always get a result line
        traceback.print_exc(file=sys.stderr)
        result = {"run_id": task.get("run_id"), "outcome": "error", "errors": [f"{type(e).__name__}: {e}"]}
    sys.stdout.write(RESULT_MARKER + json.dumps(result, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    cli()
