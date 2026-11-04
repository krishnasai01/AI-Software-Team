"""The job queue: every agent run is a row, claimed with FOR UPDATE SKIP LOCKED and held with a
20-minute lease renewed by heartbeat. A lapsed lease (dead worker) puts the job back in the queue.
At most one live job per ticket, enforced by a unique index."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from orchestrator.state import q, q1

LEASE = timedelta(minutes=20)
BACKOFF_MINUTES = (1, 3, 9)  # after a crashed run: 1st, 2nd, 3rd retry


@dataclass
class Job:
    id: int
    kind: str
    mode: str
    product: str
    ticket: int | None
    pr: int | None
    head_sha: str | None
    status: str
    attempt: int
    max_attempts: int
    input: dict
    result: dict | None
    error: str | None
    worker: str | None
    created_at: datetime
    finished_at: datetime | None

    @property
    def outcome(self) -> str | None:
        return (self.result or {}).get("outcome")

    @classmethod
    def from_row(cls, r: dict) -> "Job":
        return cls(**{k: r[k] for k in cls.__dataclass_fields__})


async def enqueue(
    kind: str,
    mode: str,
    product: str,
    ticket: int | None,
    pr: int | None = None,
    head_sha: str | None = None,
    input: dict | None = None,
    max_attempts: int = 3,
) -> int | None:
    """Queue a job. None if the ticket (or outsider PR) already has a live job."""
    try:
        row = await q1(
            """INSERT INTO jobs (kind, mode, product, ticket, pr, head_sha, input, max_attempts)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (kind, mode, product, ticket, pr, head_sha, Jsonb(input or {}), max_attempts),
        )
    except UniqueViolation:
        return None
    return row["id"]


async def claim(worker: str) -> Job | None:
    row = await q1(
        """UPDATE jobs SET status = 'running', worker = %s, attempt = attempt + 1, started_at = now(),
                  heartbeat_at = now(), lease_expires_at = now() + %s
           WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND not_before <= now()
                       ORDER BY not_before, id LIMIT 1 FOR UPDATE SKIP LOCKED)
           RETURNING *""",
        (worker, LEASE),
    )
    return Job.from_row(row) if row else None


async def heartbeat(job_id: int, worker: str) -> bool:
    """Renew the lease. False if the job was cancelled or taken over: the caller must stop."""
    row = await q1(
        """UPDATE jobs SET heartbeat_at = now(), lease_expires_at = now() + %s
           WHERE id = %s AND worker = %s AND status = 'running' RETURNING id""",
        (LEASE, job_id, worker),
    )
    return row is not None


async def succeed(job_id: int, worker: str, result: dict) -> None:
    await q(
        """UPDATE jobs SET status = 'succeeded', result = %s, finished_at = now(), lease_expires_at = NULL
           WHERE id = %s AND worker = %s AND status = 'running'""",
        (Jsonb(result), job_id, worker),
    )


async def retry_or_fail(job: Job, worker: str, error: str, result: dict | None = None) -> str:
    """After a crash: back to the queue with backoff, or failed once attempts are used up."""
    if job.attempt < job.max_attempts:
        delay = timedelta(minutes=BACKOFF_MINUTES[min(job.attempt, len(BACKOFF_MINUTES)) - 1])
        await q(
            """UPDATE jobs SET status = 'queued', error = %s, not_before = now() + %s, lease_expires_at = NULL,
                      worker = NULL WHERE id = %s AND worker = %s AND status = 'running'""",
            (error[:2000], delay, job.id, worker),
        )
        return "queued"
    await q(
        """UPDATE jobs SET status = 'failed', error = %s, result = %s, finished_at = now(), lease_expires_at = NULL
           WHERE id = %s AND worker = %s AND status = 'running'""",
        (error[:2000], Jsonb(result or {"outcome": "error"}), job.id, worker),
    )
    return "failed"


async def fail(job: Job, worker: str, error: str) -> None:
    """Fail without retrying (for example, a GitHub App lacks a permission)."""
    await q(
        """UPDATE jobs SET status = 'failed', error = %s, result = %s, finished_at = now(), lease_expires_at = NULL
           WHERE id = %s AND worker = %s AND status = 'running'""",
        (error[:2000], Jsonb({"outcome": "error", "error": error[:2000]}), job.id, worker),
    )


async def defer(job: Job, worker: str, delay: timedelta, reason: str) -> None:
    """Put a job back without using up an attempt (budget exhausted, capacity, ...)."""
    await q(
        """UPDATE jobs SET status = 'queued', attempt = attempt - 1, error = %s, not_before = now() + %s,
                  lease_expires_at = NULL, worker = NULL WHERE id = %s AND worker = %s AND status = 'running'""",
        (reason, delay, job.id, worker),
    )


async def cancel_live(product: str, ticket: int, reason: str) -> int:
    rows = await q(
        """UPDATE jobs SET status = 'cancelled', error = %s, finished_at = now(), lease_expires_at = NULL
           WHERE product = %s AND ticket = %s AND status IN ('queued', 'running') RETURNING id""",
        (reason, product, ticket),
    )
    return len(rows)


async def requeue_expired() -> list[dict]:
    """Jobs whose worker stopped heartbeating: requeue, or fail once attempts are used up."""
    return await q(
        """UPDATE jobs SET status = CASE WHEN attempt < max_attempts THEN 'queued' ELSE 'failed' END,
                  finished_at = CASE WHEN attempt < max_attempts THEN NULL ELSE now() END,
                  error = 'lease expired (worker stopped heartbeating)', lease_expires_at = NULL, worker = NULL
           WHERE status = 'running' AND lease_expires_at < now() RETURNING id, status, product, ticket"""
    )


async def live(product: str, ticket: int) -> Job | None:
    row = await q1(
        "SELECT * FROM jobs WHERE product = %s AND ticket = %s AND status IN ('queued', 'running')", (product, ticket)
    )
    return Job.from_row(row) if row else None


async def history(product: str, ticket: int, limit: int = 50) -> list[Job]:
    """Finished jobs for the ticket, newest first."""
    rows = await q(
        """SELECT * FROM jobs WHERE product = %s AND ticket = %s AND status IN ('succeeded', 'failed')
           ORDER BY id DESC LIMIT %s""",
        (product, ticket, limit),
    )
    return [Job.from_row(r) for r in rows]


async def get(job_id: int) -> Job | None:
    row = await q1("SELECT * FROM jobs WHERE id = %s", (job_id,))
    return Job.from_row(row) if row else None


async def tickets_with_live_jobs(product: str) -> list[int]:
    rows = await q(
        """SELECT DISTINCT ticket FROM jobs
           WHERE product = %s AND ticket IS NOT NULL AND status IN ('queued', 'running')""",
        (product,),
    )
    return [r["ticket"] for r in rows]


async def reviewed(product: str, pr: int, head_sha: str) -> bool:
    row = await q1(
        """SELECT 1 FROM jobs WHERE product = %s AND pr = %s AND head_sha = %s AND kind = 'reviewer'
           AND status IN ('queued', 'running', 'succeeded') LIMIT 1""",
        (product, pr, head_sha),
    )
    return row is not None
