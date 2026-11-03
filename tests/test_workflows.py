"""TicketWorkflow and ReviewWorkflow against Temporal's time-skipping test server, with fake activities."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.activities.github import ClaimInput, OpenPrInput, TicketPrInput
from orchestrator.types import (
    AGENT_QUEUE,
    ORCHESTRATION_QUEUE,
    AgentRunInput,
    AgentRunResult,
    NeedsHuman,
    PullRequest,
    ReviewInput,
    ReviewOutcome,
    TicketInput,
    ticket_workflow_id,
)
from orchestrator.workflows.review import ReviewWorkflow
from orchestrator.workflows.ticket import TicketWorkflow

REPO = "acme/shop"


class Fakes:
    """Records calls; behaviour is set per test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.claim_ok = True
        self.engineer_results: list[AgentRunResult | Exception] = []
        self.reviewer_output: dict | None = {"verdict": "approve", "summary": "LGTM", "comments": []}
        self.ci = "success: all checks passed"
        self.sha_counter = 0

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def activities(self) -> tuple[list, list]:
        f = self

        @activity.defn(name="claim_ticket")
        async def claim_ticket(inp: ClaimInput) -> bool:
            f.calls.append(("claim_ticket", inp))
            return f.claim_ok

        @activity.defn(name="park_claim")
        async def park_claim(inp: ClaimInput) -> None:
            f.calls.append(("park_claim", inp))

        @activity.defn(name="release_claim")
        async def release_claim(inp: ClaimInput) -> None:
            f.calls.append(("release_claim", inp))

        @activity.defn(name="open_pr")
        async def open_pr(inp: OpenPrInput) -> PullRequest:
            f.calls.append(("open_pr", inp))
            return PullRequest(7, f"https://github.com/{REPO}/pull/7", inp.result.head_sha)

        @activity.defn(name="needs_human")
        async def needs_human(inp: NeedsHuman) -> None:
            f.calls.append(("needs_human", inp))

        @activity.defn(name="notify_merge_ready")
        async def notify_merge_ready(inp: TicketPrInput) -> None:
            f.calls.append(("notify_merge_ready", inp))

        @activity.defn(name="remind_merge")
        async def remind_merge(inp: TicketPrInput) -> None:
            f.calls.append(("remind_merge", inp))

        @activity.defn(name="finish_ticket")
        async def finish_ticket(inp: TicketPrInput) -> None:
            f.calls.append(("finish_ticket", inp))

        @activity.defn(name="wait_for_ci")
        async def wait_for_ci(inp: ReviewInput) -> str:
            f.calls.append(("wait_for_ci", inp))
            return f.ci

        @activity.defn(name="post_review")
        async def post_review(outcome: ReviewOutcome, repo: str) -> None:
            f.calls.append(("post_review", outcome))

        @activity.defn(name="run_agent")
        async def run_agent(inp: AgentRunInput) -> AgentRunResult:
            f.calls.append((f"run_{inp.role}", inp))
            if inp.role == "reviewer":
                return AgentRunResult("rev-1", "completed", output=f.reviewer_output)
            nxt = f.engineer_results.pop(0) if f.engineer_results else None
            if isinstance(nxt, Exception):
                raise nxt
            if nxt is not None:
                return nxt
            f.sha_counter += 1
            return AgentRunResult(
                f"eng-{f.sha_counter}",
                "completed",
                output={"pr_title": "t"},
                head_sha=f"sha{f.sha_counter}",
                pushed=True,
            )

        light = [
            claim_ticket,
            park_claim,
            release_claim,
            open_pr,
            needs_human,
            notify_merge_ready,
            remind_merge,
            finish_ticket,
            wait_for_ci,
            post_review,
        ]
        return light, [run_agent]


@pytest.fixture
async def env():
    e = await WorkflowEnvironment.start_time_skipping()
    yield e
    await e.shutdown()


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
async def client(env, fakes):
    light, heavy = fakes.activities()
    async with (
        Worker(
            env.client, task_queue=ORCHESTRATION_QUEUE, workflows=[TicketWorkflow, ReviewWorkflow], activities=light
        ),
        Worker(env.client, task_queue=AGENT_QUEUE, activities=heavy),
    ):
        yield env.client


async def start_ticket(client: Client, number: int = 42):
    return await client.start_workflow(
        TicketWorkflow.run,
        TicketInput(REPO, number),
        id=ticket_workflow_id(REPO, number),
        task_queue=ORCHESTRATION_QUEUE,
    )


async def wait_until(pred, timeout: float = 10):
    """Poll real time without letting the test server skip ahead (it skips only while we await results)."""
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


def review(verdict: str, sha: str, summary: str = "") -> ReviewOutcome:
    return ReviewOutcome(verdict, summary or verdict, 7, sha)


async def test_happy_path_approve_then_merge(client, fakes):
    h = await start_ticket(client)
    await wait_until(lambda: "park_claim" in fakes.names())
    await h.signal(TicketWorkflow.review_submitted, review("approve", "sha1"))
    await wait_until(lambda: "notify_merge_ready" in fakes.names())
    await h.signal(TicketWorkflow.pr_closed_signal, True)
    assert await h.result() == "merged"
    assert fakes.names() == [
        "claim_ticket",
        "run_engineer",
        "open_pr",
        "park_claim",
        "notify_merge_ready",
        "finish_ticket",
        "release_claim",
    ]


async def test_rework_round_passes_feedback_and_ignores_stale_reviews(client, fakes):
    h = await start_ticket(client)
    await wait_until(lambda: "park_claim" in fakes.names())
    await h.signal(TicketWorkflow.review_submitted, review("request_changes", "sha1", "add a test for empty cart"))
    await wait_until(lambda: fakes.names().count("park_claim") == 2)
    # A late approval for the old commit must not count for the new one.
    await h.signal(TicketWorkflow.review_submitted, review("approve", "sha1"))
    await asyncio.sleep(0.3)
    assert "notify_merge_ready" not in fakes.names()
    await h.signal(TicketWorkflow.review_submitted, review("approve", "sha2"))
    await wait_until(lambda: "notify_merge_ready" in fakes.names())
    await h.signal(TicketWorkflow.pr_closed_signal, True)
    assert await h.result() == "merged"
    second_run = [c for n, c in fakes.calls if n == "run_engineer"][1]
    assert "add a test for empty cart" in second_run.feedback


async def test_three_rejections_escalate_to_human(client, fakes):
    h = await start_ticket(client)
    for i in range(1, 4):
        await wait_until(lambda i=i: fakes.names().count("park_claim") == i)
        await h.signal(TicketWorkflow.review_submitted, review("request_changes", f"sha{i}"))
    assert await h.result() == "needs-human"
    assert fakes.names().count("run_engineer") == 3
    reason = next(c for n, c in fakes.calls if n == "needs_human").reason
    assert "3 review rounds" in reason
    assert fakes.names()[-1] == "release_claim"


async def test_blocked_engineer_goes_to_human_without_a_pr(client, fakes):
    fakes.engineer_results = [AgentRunResult("eng-1", "blocked", output={"open_questions": ["Which currency?"]})]
    h = await start_ticket(client)
    assert await h.result() == "needs-human"
    assert "open_pr" not in fakes.names()
    assert "Which currency?" in next(c for n, c in fakes.calls if n == "needs_human").reason


async def test_budget_exhaustion_is_not_retried(client, fakes):
    fakes.engineer_results = [ApplicationError("engineer spent $50", type="BudgetExceeded", non_retryable=True)]
    h = await start_ticket(client)
    assert await h.result() == "needs-human"
    assert fakes.names().count("run_engineer") == 1
    assert "engineer spent $50" in next(c for n, c in fakes.calls if n == "needs_human").reason


async def test_crashed_run_is_retried_twice_then_escalates(client, fakes):
    crash = ApplicationError("sandbox died", type="AgentCrashed")
    fakes.engineer_results = [crash, crash, crash]
    h = await start_ticket(client)
    assert await h.result() == "needs-human"
    assert fakes.names().count("run_engineer") == 3


async def test_no_review_within_a_day_escalates(client, fakes):
    h = await start_ticket(client)
    assert await h.result() == "needs-human"  # the test server skips the 24 hours
    assert "No review arrived" in next(c for n, c in fakes.calls if n == "needs_human").reason


async def test_merge_wait_sends_reminders_and_never_auto_approves(env, client, fakes):
    h = await start_ticket(client)
    await wait_until(lambda: "park_claim" in fakes.names())
    await h.signal(TicketWorkflow.review_submitted, review("approve", "sha1"))
    await wait_until(lambda: "notify_merge_ready" in fakes.names())
    await env.sleep(timedelta(hours=9))
    assert fakes.names().count("remind_merge") == 2
    assert "finish_ticket" not in fakes.names()
    await h.signal(TicketWorkflow.pr_closed_signal, True)
    assert await h.result() == "merged"


async def test_pr_closed_without_merge_escalates(client, fakes):
    h = await start_ticket(client)
    await wait_until(lambda: "park_claim" in fakes.names())
    await h.signal(TicketWorkflow.pr_closed_signal, False)
    assert await h.result() == "needs-human"


async def test_cancelled_ticket_still_releases_its_claim(client, fakes):
    h = await start_ticket(client)
    await wait_until(lambda: "park_claim" in fakes.names())
    await h.cancel()
    with pytest.raises(WorkflowFailureError):
        await h.result()
    assert fakes.names()[-1] == "release_claim"


async def test_already_claimed_ticket_does_nothing(client, fakes):
    fakes.claim_ok = False
    h = await start_ticket(client)
    assert await h.result() == "claimed-elsewhere"
    assert fakes.names() == ["claim_ticket"]


async def test_review_workflow_signals_ticket(client, fakes):
    h = await start_ticket(client)
    await wait_until(lambda: "park_claim" in fakes.names())
    verdict = await client.execute_workflow(
        ReviewWorkflow.run,
        ReviewInput(REPO, 7, "sha1", 42),
        id=f"review-{uuid.uuid4()}",
        task_queue=ORCHESTRATION_QUEUE,
    )
    assert verdict == "approve"
    assert "post_review" in fakes.names()
    await wait_until(lambda: "notify_merge_ready" in fakes.names())
    await h.signal(TicketWorkflow.pr_closed_signal, True)
    assert await h.result() == "merged"


async def test_failing_ci_requests_changes_without_spending_tokens(client, fakes):
    fakes.ci = "failure: unit-tests"
    verdict = await client.execute_workflow(
        ReviewWorkflow.run,
        ReviewInput(REPO, 9, "abc", None),
        id=f"review-{uuid.uuid4()}",
        task_queue=ORCHESTRATION_QUEUE,
    )
    assert verdict == "request_changes"
    assert "run_reviewer" not in fakes.names()
    posted = next(c for n, c in fakes.calls if n == "post_review")
    assert "unit-tests" in posted.summary


async def test_merge_closing_the_issue_first_still_records_the_merge(client, fakes):
    h = await start_ticket(client)
    await wait_until(lambda: "park_claim" in fakes.names())
    await h.signal(TicketWorkflow.review_submitted, review("approve", "sha1"))
    await wait_until(lambda: "notify_merge_ready" in fakes.names())
    # "Closes #42" closes the issue in the same instant as the merge; the issue event wins the race.
    await h.signal(TicketWorkflow.issue_closed_signal)
    await h.signal(TicketWorkflow.pr_closed_signal, True)
    assert await h.result() == "merged"
    assert "finish_ticket" in fakes.names()


async def test_issue_closed_by_hand_stops_the_ticket(client, fakes):
    h = await start_ticket(client)
    await wait_until(lambda: "park_claim" in fakes.names())
    await h.signal(TicketWorkflow.issue_closed_signal)
    assert await h.result() == "issue-closed"  # after the grace period, skipped by the test server
    assert "needs_human" not in fakes.names() and "finish_ticket" not in fakes.names()
    assert fakes.names()[-1] == "release_claim"
