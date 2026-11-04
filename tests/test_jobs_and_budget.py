"""The Postgres job queue and the budget, against a real database."""

from datetime import timedelta

import pytest

from tests.conftest import needs_db

pytestmark = [pytest.mark.db, needs_db]


async def test_one_live_job_per_ticket(db):
    from orchestrator import jobs

    first = await jobs.enqueue("engineer", "implement", "shop", 1)
    assert first is not None
    assert await jobs.enqueue("reviewer", "review", "shop", 1) is None  # one live job per ticket
    assert await jobs.enqueue("engineer", "implement", "shop", 2) is not None
    assert await jobs.enqueue("reviewer", "review", "shop", None, pr=9, head_sha="a") is not None
    assert await jobs.enqueue("reviewer", "review", "shop", None, pr=9, head_sha="b") is None


async def test_claim_heartbeat_and_lease_loss(db):
    from orchestrator import jobs, state

    await jobs.enqueue("engineer", "implement", "shop", 1)
    job = await jobs.claim("w1")
    assert job.status == "running" and job.attempt == 1 and job.worker == "w1"
    assert await jobs.claim("w2") is None  # nothing else queued
    assert await jobs.heartbeat(job.id, "w1")
    assert not await jobs.heartbeat(job.id, "w2")  # a stranger cannot hold it

    await jobs.cancel_live("shop", 1, "issue closed")
    assert not await jobs.heartbeat(job.id, "w1")  # the worker learns to stop
    await state.q(
        "UPDATE jobs SET status = 'running', lease_expires_at = now() - interval '1 minute' WHERE id = %s", (job.id,)
    )
    assert [r["status"] for r in await jobs.requeue_expired()] == ["queued"]


async def test_crash_retries_with_backoff_then_fails(db):
    from orchestrator import jobs, state

    await jobs.enqueue("engineer", "implement", "shop", 1)
    for attempt in (1, 2):
        job = await jobs.claim("w")
        assert job.attempt == attempt
        assert await jobs.retry_or_fail(job, "w", "sandbox died") == "queued"
        assert await jobs.claim("w") is None  # backing off
        await state.q("UPDATE jobs SET not_before = now() WHERE id = %s", (job.id,))
    job = await jobs.claim("w")
    assert await jobs.retry_or_fail(job, "w", "sandbox died") == "failed"
    [failed] = await jobs.history("shop", 1)
    assert failed.status == "failed" and failed.outcome == "error"


async def test_defer_does_not_use_an_attempt(db):
    from orchestrator import jobs

    await jobs.enqueue("engineer", "implement", "shop", 1)
    job = await jobs.claim("w")
    await jobs.defer(job, "w", timedelta(0), "budget")
    again = await jobs.claim("w")
    assert again.attempt == 1


async def test_budget_caps_borrowing_and_ceiling(db):
    from core import budget
    from orchestrator import state

    # Under 70% of the $250 ceiling a role may borrow up to 150% of its cap.
    assert await budget.effective_cap("engineer") == 75
    await budget.record("engineer", 60)
    assert await budget.check("engineer") == 15
    await budget.record("engineer", 15)
    with pytest.raises(budget.BudgetExceeded, match="cap for today"):
        await budget.check("engineer")
    # Once the team is past 70% of the ceiling, borrowing stops.
    await budget.record("techlead", 110)
    assert await budget.effective_cap("reviewer") == 15
    # The ceiling is a control a human sets; agents cannot reach it.
    await state.set_control("ceiling_usd", 180, "human:test")
    with pytest.raises(budget.BudgetExceeded, match="ceiling"):
        await budget.check("reviewer")


async def test_kill_switch_and_questions(db):
    from orchestrator import state

    assert await state.pause_state("shop") is None
    await state.pause("shop", "looking into it", "human:test")
    assert "shop paused" in await state.pause_state("shop")
    await state.pause(None, "spend spike", "watchdog")
    assert "all agents paused" in await state.pause_state("other")
    await state.resume(None, "human:test")
    await state.resume("shop", "human:test")
    assert await state.pause_state("shop") is None

    qid = await state.create_question("shop", 1, "Which currency?", ["EUR", "USD"], "EUR", True, None)
    assert await state.create_question("shop", 1, "Another?", [], None, False, None) is None  # one open at a time
    row = await state.resolve_question(qid, "answered", "USD", "owner")
    assert row["answer"] == "USD"
    assert await state.resolve_question(qid, "answered", "EUR", "owner") is None
