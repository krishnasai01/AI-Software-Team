"""A scripted agent for rehearsals: `python -m agents.rehearsal` in place of the runner.

It goes through exactly the same sandbox plumbing as a real run (task file, repo bundle, commits
exported as a bundle, result file) but makes a trivial, deterministic change instead of calling a
model. Register a scratch product with the `agent-company/sandbox-rehearsal` image to exercise
GitHub Apps, webhooks, push, review, the merge policy, auto-merge and reverts for $0.
"""

import json
from pathlib import Path

from agents.runner import OUT_DIR, TASK_DIR, export_commits, git, prepare_workspace

WORKSPACE = Path("/workspace")


def scripted(task: dict) -> dict:
    start = prepare_workspace(task)
    role, number = task["role"], task["ticket"]["number"]
    result: dict = {
        "run_id": task["run_id"],
        "subtype": "success",
        "ok": True,
        "turns": 1,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "errors": [],
        "denials": [],
        "start_sha": start,
    }
    if role == "engineer":
        notes = WORKSPACE / "docs" / "rehearsal.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        with notes.open("a") as f:
            f.write(f"- rehearsal run {task['run_id']} for #{number} ({task['mode']})\n")
        git("add", "-A")
        git("commit", "--quiet", "-m", f"docs: rehearsal note for #{number}")
        result["output"] = {
            "status": "completed",
            "summary": "Rehearsal: appended a line to docs/rehearsal.md.",
            "pr_title": f"Rehearsal for #{number}",
            "pr_body": f"Rehearsal change.\n\nCloses #{number}",
            "tests_run": [{"command": "true", "passed": True}],
            "open_questions": [],
        }
        result.update(export_commits(start))
    elif role == "reviewer":
        result["output"] = {
            "verdict": "approve",
            "summary": "Rehearsal review: approved.",
            "comments": [],
            "risk": "low",
            "injection_suspected": False,
        }
    else:
        result["output"] = {
            "decision": "answer",
            "guidance": "Rehearsal: go ahead.",
            "sub_tickets": [],
            "question": {"question": "", "options": [], "default": "", "reversible": False},
            "risk_flag": "none",
        }
    return result


def cli() -> None:
    task = json.loads((TASK_DIR / "task.json").read_text())
    try:
        result = scripted(task)
    except Exception as e:  # noqa: BLE001
        result = {"run_id": task.get("run_id"), "subtype": "runner_error", "ok": False, "errors": [str(e)]}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    cli()
