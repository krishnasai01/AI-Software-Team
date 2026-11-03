"""ReviewWorkflow: one per PR head commit, id `review-<owner>-<repo>-<pr>-<sha>`.

Waits for CI, runs the reviewer (or requests changes straight away when CI fails, which costs no
tokens), posts the review, and signals the ticket's workflow.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestrator.types import (
        AGENT_QUEUE,
        AgentRunInput,
        AgentRunResult,
        ReviewInput,
        ReviewOutcome,
        ticket_workflow_id,
    )
    from orchestrator.workflows.ticket import LIGHT, TicketWorkflow

CI_WAIT = dict(
    schedule_to_close_timeout=timedelta(minutes=60),
    retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=30),
        backoff_coefficient=1.5,
        maximum_interval=timedelta(minutes=2),
        non_retryable_error_types=["GitHubPermissionError"],
    ),
)
REVIEWER = dict(
    task_queue=AGENT_QUEUE,
    start_to_close_timeout=timedelta(minutes=40),
    heartbeat_timeout=timedelta(minutes=3),
    retry_policy=RetryPolicy(
        maximum_attempts=3,
        initial_interval=timedelta(minutes=1),
        backoff_coefficient=3,
        non_retryable_error_types=["BudgetExceeded", "ProductNotFound", "GitHubPermissionError"],
    ),
)


@workflow.defn
class ReviewWorkflow:
    @workflow.run
    async def run(self, inp: ReviewInput) -> str:
        try:
            ci = await workflow.execute_activity("wait_for_ci", inp, result_type=str, **CI_WAIT)
        except ActivityError:
            ci = "unknown: CI did not finish within 60 minutes"

        if ci.startswith("failure"):
            outcome = ReviewOutcome(
                "request_changes",
                f"CI is failing ({ci.removeprefix('failure: ')}). Make all checks pass before review.",
                inp.pr_number,
                inp.head_sha,
            )
        else:
            outcome = await self._run_reviewer(inp, ci)

        if outcome.verdict in ("approve", "request_changes"):
            await workflow.execute_activity("post_review", args=[outcome, inp.repo], **LIGHT)

        if inp.ticket_number is not None:
            handle = workflow.get_external_workflow_handle(ticket_workflow_id(inp.repo, inp.ticket_number))
            try:
                await handle.signal(TicketWorkflow.review_submitted, outcome)
            except Exception as e:  # noqa: BLE001 - the ticket workflow may have finished
                workflow.logger.info("could not signal ticket workflow: %s", e)
        return outcome.verdict

    async def _run_reviewer(self, inp: ReviewInput, ci: str) -> ReviewOutcome:
        run_input = AgentRunInput(
            "reviewer",
            inp.repo,
            inp.ticket_number or 0,
            workflow.info().workflow_id,
            pr_number=inp.pr_number,
            head_sha=inp.head_sha,
            ci_summary=ci,
        )
        try:
            result: AgentRunResult = await workflow.execute_activity(
                "run_agent", run_input, result_type=AgentRunResult, **REVIEWER
            )
        except ActivityError as e:
            return ReviewOutcome("error", f"reviewer run failed: {e.cause or e}", inp.pr_number, inp.head_sha)
        out = result.output or {}
        if result.outcome != "completed" or out.get("verdict") not in ("approve", "request_changes"):
            return ReviewOutcome("error", f"reviewer run ended with {result.outcome}", inp.pr_number, inp.head_sha)
        return ReviewOutcome(
            out["verdict"], out.get("summary", ""), inp.pr_number, inp.head_sha, comments=out.get("comments") or []
        )
