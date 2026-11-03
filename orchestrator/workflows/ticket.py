"""TicketWorkflow: one per ticket, id `ticket-<owner>-<repo>-<issue>`.

claim -> engineer run -> PR -> wait for review (rework up to 3 rounds) -> ask you to merge -> wait
for the merge -> release. Any failure, block or stall hands the ticket to a human.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestrator.activities.github import ClaimInput, OpenPrInput, TicketPrInput
    from orchestrator.types import (
        AGENT_QUEUE,
        MAX_REVIEW_ROUNDS,
        AgentRunInput,
        AgentRunResult,
        NeedsHuman,
        PullRequest,
        ReviewOutcome,
        TicketInput,
    )

LIGHT = dict(start_to_close_timeout=timedelta(minutes=2), retry_policy=RetryPolicy(maximum_attempts=5))
ENGINEER = dict(
    task_queue=AGENT_QUEUE,
    start_to_close_timeout=timedelta(minutes=100),
    heartbeat_timeout=timedelta(minutes=3),
    # A crashed run is retried twice with backoff, then the ticket goes to a human.
    retry_policy=RetryPolicy(
        maximum_attempts=3,
        initial_interval=timedelta(minutes=1),
        backoff_coefficient=3,
        non_retryable_error_types=["BudgetExceeded", "ProductNotFound", "GitHubPermissionError"],
    ),
)
REVIEW_WAIT = timedelta(hours=24)
MERGE_REMINDER = timedelta(hours=4)
# A merge closes the issue ("Closes #N") at the same moment as the PR; give the PR event time to land.
ISSUE_CLOSED_GRACE = timedelta(minutes=5)


def format_feedback(review: ReviewOutcome) -> str:
    lines = [review.summary]
    lines += [f"- {c['path']}:{c['line']}: {c['body']}" for c in review.comments]
    return "\n".join(lines)


@workflow.defn
class TicketWorkflow:
    def __init__(self) -> None:
        self.reviews: dict[str, ReviewOutcome] = {}  # by PR head sha, so a stale review never counts
        self.pr_closed: bool | None = None  # True if merged
        self.issue_closed = False

    @workflow.signal
    def review_submitted(self, review: ReviewOutcome) -> None:
        self.reviews[review.head_sha] = review

    @workflow.signal
    def pr_closed_signal(self, merged: bool) -> None:
        self.pr_closed = merged

    @workflow.signal
    def issue_closed_signal(self) -> None:
        self.issue_closed = True

    @workflow.query
    def status(self) -> dict:
        return {"reviews": len(self.reviews), "pr_closed": self.pr_closed, "issue_closed": self.issue_closed}

    @workflow.run
    async def run(self, inp: TicketInput) -> str:
        wf_id = workflow.info().workflow_id
        claim = ClaimInput(inp.repo, inp.number, wf_id)
        if not await workflow.execute_activity("claim_ticket", claim, result_type=bool, **LIGHT):
            return "claimed-elsewhere"
        try:
            return await self._work(inp, wf_id, claim)
        finally:
            # Runs on success, failure and cancellation alike.
            await workflow.execute_activity("release_claim", claim, **LIGHT)

    async def _needs_human(self, inp: TicketInput, wf_id: str, reason: str) -> str:
        await workflow.execute_activity("needs_human", NeedsHuman(inp.repo, inp.number, reason, wf_id), **LIGHT)
        return "needs-human"

    async def _work(self, inp: TicketInput, wf_id: str, claim: ClaimInput) -> str:
        feedback: str | None = None
        pr: PullRequest | None = None
        for round_ in range(1, MAX_REVIEW_ROUNDS + 1):
            if self.issue_closed:
                return await self._issue_closed(inp, wf_id, pr)
            try:
                result: AgentRunResult = await workflow.execute_activity(
                    "run_agent",
                    AgentRunInput("engineer", inp.repo, inp.number, wf_id, feedback=feedback),
                    result_type=AgentRunResult,
                    **ENGINEER,
                )
            except ActivityError as e:
                cause = e.cause.message if e.cause and hasattr(e.cause, "message") else str(e)
                return await self._needs_human(inp, wf_id, f"The engineer run failed: {cause}")

            if self.issue_closed:
                return await self._issue_closed(inp, wf_id, pr)
            if result.outcome != "completed" or not result.pushed:
                return await self._needs_human(inp, wf_id, self._blocked_reason(result))

            pr = await workflow.execute_activity(
                "open_pr", OpenPrInput(inp.repo, inp.number, result), result_type=PullRequest, **LIGHT
            )
            await workflow.execute_activity("park_claim", claim, **LIGHT)

            head = pr.head_sha
            try:
                await workflow.wait_condition(
                    lambda head=head: head in self.reviews or self.pr_closed is not None or self.issue_closed,
                    timeout=REVIEW_WAIT,
                )
            except TimeoutError:
                return await self._needs_human(inp, wf_id, f"No review arrived within 24 hours for {pr.url}.")

            if self.issue_closed and self.pr_closed is None:
                return await self._issue_closed(inp, wf_id, pr)
            if self.pr_closed is not None:
                return await self._closed(inp, wf_id, pr)
            review = self.reviews[head]
            if review.verdict == "approve":
                break
            if review.verdict != "request_changes":
                return await self._needs_human(inp, wf_id, f"The review of {pr.url} failed: {review.summary}")
            feedback = format_feedback(review)
            workflow.logger.info("round %d: changes requested on %s", round_, pr.url)
        else:
            return await self._needs_human(
                inp,
                wf_id,
                f"{MAX_REVIEW_ROUNDS} review rounds without approval on {pr.url}; engineer and reviewer disagree.",
            )

        # Human gate: you merge on GitHub; we wait, reminding every 4 hours, never auto-approving.
        gate = TicketPrInput(inp.repo, inp.number, pr, wf_id)
        await workflow.execute_activity("notify_merge_ready", gate, **LIGHT)
        while self.pr_closed is None:
            try:
                await workflow.wait_condition(
                    lambda: self.pr_closed is not None or self.issue_closed, timeout=MERGE_REMINDER
                )
            except TimeoutError:
                await workflow.execute_activity("remind_merge", gate, **LIGHT)
                continue
            if self.issue_closed and self.pr_closed is None:
                return await self._issue_closed(inp, wf_id, pr)
        return await self._closed(inp, wf_id, pr)

    async def _issue_closed(self, inp: TicketInput, wf_id: str, pr: PullRequest | None) -> str:
        """The issue was closed. If that was a merge, the PR event follows within seconds."""
        if pr is not None:
            try:
                await workflow.wait_condition(lambda: self.pr_closed is not None, timeout=ISSUE_CLOSED_GRACE)
                return await self._closed(inp, wf_id, pr)
            except TimeoutError:
                pass
        return "issue-closed"

    async def _closed(self, inp: TicketInput, wf_id: str, pr: PullRequest) -> str:
        if self.pr_closed:
            await workflow.execute_activity("finish_ticket", TicketPrInput(inp.repo, inp.number, pr, wf_id), **LIGHT)
            return "merged"
        return await self._needs_human(inp, wf_id, f"{pr.url} was closed without merging.")

    @staticmethod
    def _blocked_reason(result: AgentRunResult) -> str:
        out = result.output or {}
        questions = "\n".join(f"- {q}" for q in out.get("open_questions", []))
        if result.outcome == "blocked":
            why = result.note or "the engineer stopped and asked for help"
            return f"The engineer is blocked ({why}).\n{questions}".strip()
        if result.outcome in ("error_max_turns", "error_max_budget_usd", "timeout"):
            return f"The engineer run stopped without finishing ({result.outcome}); the ticket may be too large."
        if not result.pushed:
            return (
                f"The engineer finished without pushing changes ({result.note or result.outcome}).\n{questions}".strip()
            )
        return f"The engineer run ended with {result.outcome}."
