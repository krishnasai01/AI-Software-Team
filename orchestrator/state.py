"""Postgres records the system trusts, apart from the job queue (`jobs.py`)."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

from psycopg.types.json import Jsonb

from core.db import get_pool


async def q(sql: str, params: dict | tuple = ()) -> list[dict]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall() if cur.description else []


async def q1(sql: str, params: dict | tuple = ()) -> dict | None:
    rows = await q(sql, params)
    return rows[0] if rows else None


# Products


async def product(name: str) -> dict | None:
    return await q1("SELECT * FROM products WHERE name = %s", (name,))


async def product_for_repo(repo: str) -> dict | None:
    return await q1("SELECT * FROM products WHERE repo = %s AND enabled", (repo,))


async def enabled_products() -> list[dict]:
    return await q("SELECT * FROM products WHERE enabled ORDER BY id")


# Tickets: what the reconciler remembers beyond GitHub


async def ticket(product_name: str, number: int) -> dict | None:
    return await q1("SELECT * FROM tickets WHERE product = %s AND number = %s", (product_name, number))


async def upsert_ticket(product_name: str, number: int, **fields) -> None:
    cols = ["product", "number", *fields]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in fields) or "number = EXCLUDED.number"
    await q(
        f"""INSERT INTO tickets ({", ".join(cols)}) VALUES ({", ".join(["%s"] * len(cols))})
            ON CONFLICT (product, number) DO UPDATE SET {updates}, updated_at = now()""",
        (product_name, number, *fields.values()),
    )


async def children(product_name: str, parent: int) -> list[int]:
    rows = await q(
        "SELECT number FROM tickets WHERE product = %s AND parent = %s ORDER BY number", (product_name, parent)
    )
    return [r["number"] for r in rows]


# Controls: kill switch, ceiling override, once-a-day bookkeeping


async def control(key: str, default=None):
    row = await q1("SELECT value FROM controls WHERE key = %s", (key,))
    return row["value"] if row else default


async def set_control(key: str, value, by: str) -> None:
    await q(
        """INSERT INTO controls (key, value, updated_by) VALUES (%s, %s, %s)
           ON CONFLICT (key) DO UPDATE
           SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (key, Jsonb(value), by),
    )


async def pause_state(product_name: str | None = None) -> str | None:
    """Why new work may not start, or None."""
    pause = await control("pause", {})
    if pause.get("all"):
        return f"all agents paused: {pause.get('reason', '')}".strip()
    if product_name and product_name in pause.get("products", {}):
        return f"{product_name} paused: {pause['products'][product_name]}"
    return None


async def pause(product_name: str | None, reason: str, by: str) -> None:
    current = await control("pause", {})
    if product_name is None:
        current.update({"all": True, "reason": reason})
    else:
        current.setdefault("products", {})[product_name] = reason
    await set_control("pause", current, by)


async def resume(product_name: str | None, by: str) -> None:
    current = await control("pause", {})
    if product_name is None:
        current.pop("all", None)
        current.pop("reason", None)
    else:
        current.get("products", {}).pop(product_name, None)
    await set_control("pause", current, by)


# Autonomy


async def autonomy(product_name: str) -> dict:
    row = await q1("SELECT * FROM autonomy WHERE product = %s", (product_name,))
    if row is None:
        await q("INSERT INTO autonomy (product) VALUES (%s) ON CONFLICT DO NOTHING", (product_name,))
        row = await q1("SELECT * FROM autonomy WHERE product = %s", (product_name,))
    return row


async def set_autonomy(product_name: str, level: int, paused: bool, by: str, reason: str) -> None:
    await autonomy(product_name)
    await q(
        """UPDATE autonomy SET level = %s, auto_merge_paused = %s, changed_by = %s, reason = %s, changed_at = now()
           WHERE product = %s""",
        (level, paused, by, reason, product_name),
    )


# Policy decisions


async def policy_decision(product_name: str, pr: int, head_sha: str) -> dict | None:
    return await q1(
        "SELECT * FROM policy_decisions WHERE product = %s AND pr = %s AND head_sha = %s", (product_name, pr, head_sha)
    )


async def save_policy_decision(
    product_name: str,
    pr: int,
    head_sha: str,
    tier: str,
    next_tier: str,
    level: int,
    reasons: list[str],
    satisfied: bool,
) -> None:
    await q(
        """INSERT INTO policy_decisions (product, pr, head_sha, tier, next_level_tier, level, reasons, satisfied)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (product, pr, head_sha) DO UPDATE SET tier = EXCLUDED.tier,
             next_level_tier = EXCLUDED.next_level_tier, level = EXCLUDED.level, reasons = EXCLUDED.reasons,
             satisfied = EXCLUDED.satisfied""",
        (product_name, pr, head_sha, tier, next_tier, level, Jsonb(reasons), satisfied),
    )


# Merges: the track record


async def merge_record(product_name: str, pr: int) -> dict | None:
    return await q1("SELECT * FROM merges WHERE product = %s AND pr = %s", (product_name, pr))


async def record_merge(
    product_name: str,
    pr: int,
    ticket_number: int | None,
    merge_sha: str,
    auto: bool,
    level: int,
    next_tier: str | None,
    merged_at: datetime,
    watch: timedelta,
) -> None:
    await q(
        """INSERT INTO merges (product, pr, ticket, merge_sha, auto, level, next_level_tier, merged_at, watch_until)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (product_name, pr, ticket_number, merge_sha, auto, level, next_tier, merged_at, merged_at + watch),
    )


async def watched_merges(product_name: str) -> list[dict]:
    return await q("SELECT * FROM merges WHERE product = %s AND outcome IS NULL ORDER BY merged_at", (product_name,))


async def recent_merges(product_name: str, days: int = 30) -> list[dict]:
    return await q(
        "SELECT * FROM merges WHERE product = %s AND merged_at > now() - make_interval(days => %s) ORDER BY merged_at",
        (product_name, days),
    )


async def set_merge_outcome(product_name: str, pr: int, outcome: str, revert_pr: int | None = None) -> None:
    await q(
        "UPDATE merges SET outcome = %s, revert_pr = COALESCE(%s, revert_pr) WHERE product = %s AND pr = %s",
        (outcome, revert_pr, product_name, pr),
    )


# Questions for the human


async def latest_question(product_name: str, ticket_number: int) -> dict | None:
    return await q1(
        "SELECT * FROM questions WHERE product = %s AND ticket = %s ORDER BY id DESC LIMIT 1",
        (product_name, ticket_number),
    )


async def open_questions(product_name: str | None = None) -> list[dict]:
    if product_name:
        return await q("SELECT * FROM questions WHERE status = 'open' AND product = %s ORDER BY id", (product_name,))
    return await q("SELECT * FROM questions WHERE status = 'open' ORDER BY id")


async def create_question(
    product_name: str,
    ticket_number: int,
    question: str,
    options: list[str],
    default: str | None,
    reversible: bool,
    deadline: datetime | None,
) -> int | None:
    """None if the ticket already has an open question."""
    row = await q1(
        """INSERT INTO questions (product, ticket, question, options, default_answer, reversible, deadline)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
        (
            product_name,
            ticket_number,
            question,
            Jsonb(options),
            default or None,
            reversible and bool(default),
            deadline,
        ),
    )
    return row["id"] if row else None


async def resolve_question(question_id: int, status: str, answer: str, by: str) -> dict | None:
    return await q1(
        """UPDATE questions SET status = %s, answer = %s, answered_by = %s, answered_at = now()
           WHERE id = %s AND status = 'open' RETURNING *""",
        (status, answer, by, question_id),
    )


# Memories (answers and decisions agents should not have to ask for twice)


async def add_memory(
    product_name: str, kind: str, text: str, provenance: str, ticket_number: int | None, run_id: str | None = None
) -> None:
    await q(
        "INSERT INTO memories (product, kind, text, provenance, ticket, source_run) VALUES (%s, %s, %s, %s, %s, %s)",
        (product_name, kind, text, provenance, ticket_number, run_id),
    )


async def recent_memories(product_name: str, limit: int = 20) -> list[str]:
    rows = await q(
        """SELECT text, provenance FROM memories WHERE product = %s AND status = 'active'
           ORDER BY id DESC LIMIT %s""",
        (product_name, limit),
    )
    return [f"[{r['provenance']}] {r['text']}" for r in rows]


# Approvals


async def create_approval(
    kind: str, product_name: str | None, target: str, payload: dict, link: str | None
) -> int | None:
    row = await q1(
        """INSERT INTO approvals (kind, product, target, payload, chat_message_link)
           SELECT %s, %s, %s, %s, %s
           WHERE NOT EXISTS (SELECT 1 FROM approvals WHERE kind = %s AND target = %s AND status = 'pending')
           RETURNING id""",
        (kind, product_name, target, Jsonb(payload), link, kind, target),
    )
    return row["id"] if row else None


async def pending_approvals() -> list[dict]:
    return await q("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id")


async def decide_approval_by_target(kind: str, target: str, approved: bool, by: str) -> None:
    await q(
        """UPDATE approvals SET status = %s, decided_by = %s, decided_at = now()
           WHERE kind = %s AND target = %s AND status = 'pending'""",
        ("approved" if approved else "rejected", by, kind, target),
    )


async def decide_approval(approval_id: int, approved: bool, by: str) -> dict | None:
    return await q1(
        """UPDATE approvals SET status = %s, decided_by = %s, decided_at = now()
           WHERE id = %s AND status = 'pending' RETURNING *""",
        ("approved" if approved else "rejected", by, approval_id),
    )


# Incidents, runs, webhook events


async def record_incident(kind: str, action: str, agent: str | None = None, ticket: str | None = None) -> None:
    await q(
        "INSERT INTO incidents (kind, agent, ticket, action_taken) VALUES (%s, %s, %s, %s)",
        (kind, agent, ticket, action),
    )


async def start_run(run_id: str, agent: str, ticket_ref: str | None, job_id: int, trace_url: str | None) -> None:
    await q(
        "INSERT INTO runs (id, agent, ticket, job_id, trace_url) VALUES (%s, %s, %s, %s, %s)",
        (run_id, agent, ticket_ref, job_id, trace_url),
    )


async def finish_run(run_id: str, outcome: str, turns: int, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
    await q(
        """UPDATE runs SET ended_at = now(), outcome = %s, turns = %s, tokens_in = %s, tokens_out = %s, cost_usd = %s
           WHERE id = %s""",
        (outcome, turns, tokens_in, tokens_out, Decimal(str(round(cost_usd, 4))), run_id),
    )


async def store_event(delivery_id: str, event: str, payload: dict) -> bool:
    """False if this delivery was already received (GitHub redelivery)."""
    rows = await q(
        """INSERT INTO events_inbox (delivery_id, event, payload) VALUES (%s, %s, %s)
           ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id""",
        (delivery_id, event, Jsonb(payload)),
    )
    return bool(rows)


async def take_events(limit: int = 500) -> list[dict]:
    """Unprocessed webhook events, marked processed as they are taken (they are only nudges)."""
    return await q(
        """UPDATE events_inbox SET processed_at = now()
           WHERE delivery_id IN (SELECT delivery_id FROM events_inbox WHERE processed_at IS NULL
                                 ORDER BY received_at LIMIT %s FOR UPDATE SKIP LOCKED)
           RETURNING event, payload""",
        (limit,),
    )


def dumps(value) -> str:
    return json.dumps(value, default=str)
