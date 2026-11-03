"""The `run_agent(role, ticket)` activity: one agent session in one fresh sandbox.

Order matters: check budget, mint short-lived credentials, start the sandbox and heartbeat while it
runs, then always revoke the model key and record cost, even if the run failed.
"""

import uuid

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agents.runner import load_role
from core import budget
from core.model import RunKeys
from core.sandbox import DockerSandbox, SandboxSpec
from core.settings import get_settings
from core.tickets import GitHubApp, TicketRef, Tickets
from orchestrator import state
from orchestrator.types import AgentRunInput, AgentRunResult

# Scope each sandbox's GitHub token below its App's own permissions.
SANDBOX_GITHUB_PERMISSIONS = {
    "engineer": {"contents": "write", "pull_requests": "read", "issues": "read"},
    "reviewer": {"contents": "read", "pull_requests": "read", "issues": "read"},
}


@activity.defn(name="run_agent")
async def run_agent_activity(inp: AgentRunInput) -> AgentRunResult:
    s = get_settings()
    product = await state.product_for_repo(inp.repo)
    if product is None:
        raise ApplicationError(f"{inp.repo} is not a registered product", type="ProductNotFound", non_retryable=True)
    try:
        remaining = await budget.check(inp.role)
    except budget.BudgetExceeded as e:
        raise ApplicationError(str(e), type="BudgetExceeded", non_retryable=True) from e

    config, _ = load_role(inp.role)
    per_run = float(min(config["max_budget_usd_per_run"], remaining))
    run_id = f"{inp.role}-{uuid.uuid4().hex[:16]}"
    ticket = TicketRef(inp.repo, inp.ticket_number)
    tickets = Tickets("orchestrator")

    if inp.pr_number is not None:
        pr = await tickets.get_pr(inp.repo, inp.pr_number)
        issue = await tickets.get_issue(ticket) if inp.ticket_number else pr
    else:
        pr, issue = None, await tickets.get_issue(ticket)

    gh_token = await GitHubApp(inp.role).token(inp.repo, SANDBOX_GITHUB_PERMISSIONS[inp.role])
    keys = RunKeys()
    model_key = await keys.issue(
        run_id, [config["model"]], per_run, hours=max(2, config["run_timeout_minutes"] // 60 + 1)
    )
    trace_url = f"{s.langfuse_url.rstrip('/')}/trace/{run_id}" if s.langfuse_url else None
    await state.start_run(run_id, inp.role, str(ticket) if inp.ticket_number else None, inp.workflow_id, trace_url)

    task = {
        "run_id": run_id,
        "role": inp.role,
        "repo": inp.repo,
        "base_branch": product["default_branch"],
        "branch": ticket.branch,
        "ticket": {"number": issue["number"], "title": issue["title"], "body": issue.get("body") or ""},
        "feedback": inp.feedback,
        "budget_remaining_usd": per_run,
        "git_author": {"name": f"agent-company-{inp.role}[bot]", "email": f"{inp.role}@agents.noreply.local"},
        "secrets": {"github_token": gh_token, "model_base_url": s.sandbox_litellm_url, "model_api_key": model_key},
    }
    if pr is not None:
        task["pr"] = {"number": pr["number"], "body": pr.get("body") or "", "head_sha": inp.head_sha}
        task["ci_summary"] = inp.ci_summary or "unknown"

    holds_lease = inp.role == "engineer"

    async def beat() -> None:
        activity.heartbeat(run_id)
        if holds_lease:
            await state.heartbeat(str(ticket), inp.workflow_id)

    await beat()
    proxy_spend = None
    try:
        res = await DockerSandbox().run(
            SandboxSpec(image=product["sandbox_image"], task=task, timeout_s=config["run_timeout_minutes"] * 60),
            heartbeat=beat,
        )
    finally:
        try:
            proxy_spend = await keys.spend(model_key)
        finally:
            await keys.revoke(model_key)  # always, even if the spend lookup fails

    r = res.result or {}
    outcome = "timeout" if res.timed_out else r.get("outcome", "error")
    cost = max(proxy_spend or 0.0, float(r.get("cost_usd") or 0.0))
    await state.finish_run(
        run_id, outcome, r.get("turns") or 0, r.get("tokens_in") or 0, r.get("tokens_out") or 0, cost
    )
    spent, cap = await budget.record(inp.role, cost)
    activity.logger.info("run %s %s cost=$%.2f (%s today: $%s of $%s)", run_id, outcome, cost, inp.role, spent, cap)

    if outcome == "error" and not r.get("output"):
        # A crash (no structured result): let Temporal retry with backoff.
        detail = "; ".join(r.get("errors") or []) or res.log_tail[-1500:]
        raise ApplicationError(f"agent run {run_id} crashed: {detail}", type="AgentCrashed")

    git = r.get("git") or {}
    note = git.get("note", "")
    if git.get("blocked_paths"):
        note = f"touched protected paths: {', '.join(git['blocked_paths'])}"
    return AgentRunResult(
        run_id=run_id,
        outcome=outcome,
        output=r.get("output") if isinstance(r.get("output"), dict) else None,
        head_sha=git.get("head_sha"),
        pushed=bool(git.get("pushed")),
        cost_usd=cost,
        note=note,
    )
