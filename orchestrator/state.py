"""Postgres records the system trusts: products, claims (leases), runs, the webhook inbox,
approvals and incidents."""

from datetime import timedelta
from decimal import Decimal

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from core.db import get_pool

LEASE = timedelta(minutes=20)


async def _exec(sql: str, params: dict | tuple = ()) -> list[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall() if cur.description else []


# Products


async def product_for_repo(repo: str) -> dict | None:
    rows = await _exec("SELECT * FROM products WHERE repo = %s AND enabled", (repo,))
    return rows[0] if rows else None


async def enabled_products() -> list[dict]:
    return await _exec("SELECT * FROM products WHERE enabled ORDER BY id")


# Claims: an agent holds a ticket with a 20-minute lease, renewed by heartbeat while it works.
# While the workflow waits on review or a human, the claim is 'waiting' (owned by the live
# workflow, no lease), so a long human review never looks like a dead agent.


async def claim(ticket: str, agent: str, workflow_id: str) -> bool:
    """Take the ticket, or re-adopt our own claim after a workflow retry. False if someone else holds it."""
    try:
        await _exec(
            """INSERT INTO claims (ticket, agent, workflow_id, status, lease_expires_at, last_heartbeat)
               VALUES (%s, %s, %s, 'active', now() + %s, now())""",
            (ticket, agent, workflow_id, LEASE),
        )
        return True
    except UniqueViolation:
        rows = await _exec(
            "SELECT workflow_id FROM claims WHERE ticket = %s AND status IN ('active', 'waiting')", (ticket,)
        )
        return bool(rows) and rows[0]["workflow_id"] == workflow_id


async def heartbeat(ticket: str, workflow_id: str) -> None:
    await _exec(
        """UPDATE claims SET status = 'active', lease_expires_at = now() + %s, last_heartbeat = now()
           WHERE ticket = %s AND workflow_id = %s AND status IN ('active', 'waiting')""",
        (LEASE, ticket, workflow_id),
    )


async def park(ticket: str, workflow_id: str) -> None:
    await _exec(
        """UPDATE claims SET status = 'waiting', lease_expires_at = NULL
           WHERE ticket = %s AND workflow_id = %s AND status IN ('active', 'waiting')""",
        (ticket, workflow_id),
    )


async def release(ticket: str, workflow_id: str, status: str = "released") -> None:
    await _exec(
        """UPDATE claims SET status = %s, released_at = now(), lease_expires_at = NULL
           WHERE ticket = %s AND workflow_id = %s AND status IN ('active', 'waiting')""",
        (status, ticket, workflow_id),
    )


async def live_claims() -> list[dict]:
    return await _exec("SELECT * FROM claims WHERE status IN ('active', 'waiting')")


# Runs


async def start_run(run_id: str, agent: str, ticket: str | None, workflow_id: str, trace_url: str | None) -> None:
    await _exec(
        "INSERT INTO runs (id, agent, ticket, workflow_id, trace_url) VALUES (%s, %s, %s, %s, %s)",
        (run_id, agent, ticket, workflow_id, trace_url),
    )


async def finish_run(run_id: str, outcome: str, turns: int, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
    await _exec(
        """UPDATE runs SET ended_at = now(), outcome = %s, turns = %s, tokens_in = %s, tokens_out = %s, cost_usd = %s
           WHERE id = %s""",
        (outcome, turns, tokens_in, tokens_out, Decimal(str(round(cost_usd, 4))), run_id),
    )


# Webhook inbox


async def store_event(delivery_id: str, event: str, payload: dict) -> bool:
    """False if this delivery was already received (GitHub redelivery)."""
    rows = await _exec(
        """INSERT INTO events_inbox (delivery_id, event, payload) VALUES (%s, %s, %s)
           ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id""",
        (delivery_id, event, Jsonb(payload)),
    )
    return bool(rows)


async def mark_event(delivery_id: str, error: str | None = None) -> None:
    if error:
        await _exec("UPDATE events_inbox SET error = %s WHERE delivery_id = %s", (error[:2000], delivery_id))
    else:
        await _exec("UPDATE events_inbox SET processed_at = now(), error = NULL WHERE delivery_id = %s", (delivery_id,))


async def unprocessed_events(older_than: timedelta = timedelta(minutes=1)) -> list[dict]:
    return await _exec(
        """SELECT delivery_id, event, payload FROM events_inbox
           WHERE processed_at IS NULL AND received_at < now() - %s ORDER BY received_at LIMIT 200""",
        (older_than,),
    )


# Approvals and incidents


async def create_approval(kind: str, target: str, workflow_id: str, link: str | None) -> None:
    await _exec(
        """INSERT INTO approvals (kind, target, workflow_id, chat_message_link)
           SELECT %s, %s, %s, %s
           WHERE NOT EXISTS (SELECT 1 FROM approvals WHERE kind = %s AND target = %s AND status = 'pending')""",
        (kind, target, workflow_id, link, kind, target),
    )


async def decide_approval(kind: str, target: str, approved: bool, by: str) -> None:
    await _exec(
        """UPDATE approvals SET status = %s, decided_by = %s, decided_at = now()
           WHERE kind = %s AND target = %s AND status = 'pending'""",
        ("approved" if approved else "rejected", by, kind, target),
    )


async def record_incident(kind: str, action: str, agent: str | None = None, ticket: str | None = None) -> None:
    await _exec(
        "INSERT INTO incidents (kind, agent, ticket, action_taken) VALUES (%s, %s, %s, %s)",
        (kind, agent, ticket, action),
    )
