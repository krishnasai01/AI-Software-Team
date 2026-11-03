"""Postgres-backed state: claims and leases, budgets, the webhook inbox.

Needs TEST_DATABASE_URL pointing at a disposable database (it is wiped), e.g.
  docker run -d --rm -p 55432:5432 -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=agent \
    -e POSTGRES_DB=agent_test pgvector/pgvector:pg16
  TEST_DATABASE_URL=postgresql://agent:agent@localhost:55432/agent_test uv run pytest -m db
"""

import os

import psycopg
import pytest

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]


@pytest.fixture
async def db(monkeypatch):
    dsn = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setenv("DATABASE_URL", dsn)
    from core import db as core_db
    from core.settings import get_settings
    from db.migrate import migrate

    get_settings.cache_clear()
    assert migrate(dsn) == ["0001_init.sql"]
    assert migrate(dsn) == []  # idempotent
    yield
    await core_db.close_pool()
    get_settings.cache_clear()


async def test_one_live_claim_per_ticket(db):
    from orchestrator import state

    assert await state.claim("acme/shop#1", "engineer", "wf-a")
    assert not await state.claim("acme/shop#1", "engineer", "wf-b")
    assert await state.claim("acme/shop#1", "engineer", "wf-a")  # retried activity re-adopts its own claim
    await state.release("acme/shop#1", "wf-a")
    assert await state.claim("acme/shop#1", "engineer", "wf-b")


async def test_parked_claims_have_no_lease_and_heartbeats_renew(db):
    from core.db import get_pool
    from orchestrator import state

    await state.claim("acme/shop#1", "engineer", "wf-1")
    await state.claim("acme/shop#2", "engineer", "wf-2")
    await state.park("acme/shop#2", "wf-2")
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE claims SET lease_expires_at = now() - interval '1 minute' WHERE ticket = 'acme/shop#1'"
        )
    by_ticket = {c["ticket"]: c for c in await state.live_claims()}
    assert by_ticket["acme/shop#2"]["status"] == "waiting" and by_ticket["acme/shop#2"]["lease_expires_at"] is None
    lapsed = by_ticket["acme/shop#1"]["lease_expires_at"]

    await state.heartbeat("acme/shop#1", "wf-1")  # renews the lease
    renewed = {c["ticket"]: c for c in await state.live_claims()}["acme/shop#1"]["lease_expires_at"]
    assert renewed > lapsed
    await state.heartbeat("acme/shop#1", "wf-other")  # a stranger cannot renew it
    assert len(await state.live_claims()) == 2


async def test_budget_caps_per_agent_and_team(db, monkeypatch):
    from core import budget
    from core.settings import get_settings

    assert await budget.check("engineer") == 50
    spent, cap = await budget.record("engineer", 30.5)
    assert (spent, cap) == (30.5, 50)
    assert await budget.check("engineer") == 19.5
    await budget.record("engineer", 19.5)
    with pytest.raises(budget.BudgetExceeded, match="daily cap"):
        await budget.check("engineer")
    assert await budget.check("reviewer") == 15

    monkeypatch.setenv("TEAM_DAILY_CEILING_USD", "55")
    get_settings.cache_clear()
    await budget.record("reviewer", 5)
    with pytest.raises(budget.BudgetExceeded, match="team"):
        await budget.check("reviewer")


async def test_disabled_agent_cannot_run(db):
    from core import budget
    from core.db import get_pool

    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE agents SET enabled = false WHERE role = 'reviewer'")
    with pytest.raises(budget.BudgetExceeded, match="disabled"):
        await budget.check("reviewer")


async def test_webhook_inbox_deduplicates_and_tracks_failures(db):
    from datetime import timedelta

    from orchestrator import state

    assert await state.store_event("d-1", "issues", {"action": "labeled"})
    assert not await state.store_event("d-1", "issues", {"action": "labeled"})
    await state.mark_event("d-1", error="temporal down")
    assert [e["delivery_id"] for e in await state.unprocessed_events(older_than=timedelta(0))] == ["d-1"]
    await state.mark_event("d-1")
    assert await state.unprocessed_events(older_than=timedelta(0)) == []


async def test_runs_and_approvals(db):
    from core.db import get_pool
    from orchestrator import state

    await state.start_run("engineer-abc", "engineer", "acme/shop#1", "wf-1", None)
    await state.finish_run("engineer-abc", "completed", 42, 100_000, 8_000, 3.21)
    await state.create_approval("merge", "https://github.com/acme/shop/pull/7", "wf-1", None)
    await state.create_approval("merge", "https://github.com/acme/shop/pull/7", "wf-1", None)  # no duplicate
    await state.decide_approval("merge", "https://github.com/acme/shop/pull/7", True, "github-merge")
    pool = await get_pool()
    async with pool.connection() as conn:
        run = await (await conn.execute("SELECT * FROM runs")).fetchone()
        approvals = await (await conn.execute("SELECT status FROM approvals")).fetchall()
    assert run["outcome"] == "completed" and float(run["cost_usd"]) == 3.21 and run["turns"] == 42
    assert [a["status"] for a in approvals] == ["approved"]
