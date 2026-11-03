"""Map a GitHub event to a workflow start or signal, always by stable workflow id."""

import logging

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from core.settings import get_settings
from core.tickets import AGENT_READY, NEEDS_HUMAN, issue_number_from_branch
from orchestrator import state
from orchestrator.types import (
    ORCHESTRATION_QUEUE,
    ReviewInput,
    TicketInput,
    review_workflow_id,
    ticket_workflow_id,
)
from orchestrator.workflows.review import ReviewWorkflow
from orchestrator.workflows.ticket import TicketWorkflow

log = logging.getLogger(__name__)


async def start_ticket(client: Client, repo: str, number: int) -> bool:
    try:
        await client.start_workflow(
            TicketWorkflow.run,
            TicketInput(repo, number),
            id=ticket_workflow_id(repo, number),
            task_queue=ORCHESTRATION_QUEUE,
            # Only one run at a time per id; a finished ticket may be picked up again.
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        return True
    except WorkflowAlreadyStartedError:
        return False


async def start_review(client: Client, repo: str, pr_number: int, head_sha: str, ticket: int | None) -> bool:
    try:
        await client.start_workflow(
            ReviewWorkflow.run,
            ReviewInput(repo, pr_number, head_sha, ticket),
            id=review_workflow_id(repo, pr_number, head_sha),
            task_queue=ORCHESTRATION_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
        return True
    except WorkflowAlreadyStartedError:
        return False


def _labels(issue: dict) -> set[str]:
    return {lbl["name"] for lbl in issue.get("labels", [])}


def is_trusted(login: str) -> bool:
    return login in get_settings().trusted_labelers


async def route(client: Client, event: str, payload: dict) -> str:
    """Returns a short description of what happened (for logs and tests)."""
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo or await state.product_for_repo(repo) is None:
        return "ignored: not a registered product"
    action = payload.get("action")
    sender = (payload.get("sender") or {}).get("login", "")

    if event == "issues":
        issue = payload["issue"]
        labels = _labels(issue)
        label = (payload.get("label") or {}).get("name")
        becomes_ready = (action == "labeled" and label == AGENT_READY) or (
            action == "unlabeled" and label == NEEDS_HUMAN and AGENT_READY in labels
        )
        if becomes_ready:
            # Only you, the PM or the tech lead can put work in front of the engineers.
            if not is_trusted(sender):
                return f"ignored: {sender} may not make issues agent-ready"
            if NEEDS_HUMAN in labels or issue.get("state") != "open":
                return "ignored: issue is not ready"
            started = await start_ticket(client, repo, issue["number"])
            return "started ticket" if started else "ticket already running"
        if action == "closed":
            # A signal, not a cancel: merging a PR also closes its issue, and the workflow must still
            # record the merge. The workflow stops at its next step if the close was not a merge.
            try:
                await client.get_workflow_handle(ticket_workflow_id(repo, issue["number"])).signal(
                    TicketWorkflow.issue_closed_signal
                )
                return "signalled ticket closed"
            except RPCError:
                return "no ticket workflow to signal"
        return "ignored"

    if event == "pull_request":
        pr = payload["pull_request"]
        ticket = issue_number_from_branch(pr["head"]["ref"])
        if action in ("opened", "synchronize", "reopened", "ready_for_review"):
            if pr.get("draft"):
                return "ignored: draft"
            started = await start_review(client, repo, pr["number"], pr["head"]["sha"], ticket)
            return "started review" if started else "review already running"
        if action == "closed" and ticket is not None:
            try:
                handle = client.get_workflow_handle(ticket_workflow_id(repo, ticket))
                await handle.signal(TicketWorkflow.pr_closed_signal, bool(pr.get("merged")))
                return "signalled ticket"
            except RPCError:
                return "no ticket workflow to signal"
        return "ignored"

    return "ignored: event not handled"
