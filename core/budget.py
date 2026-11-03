"""Per-agent daily spend, the team-wide ceiling, and the check that pauses an agent at its cap.

Caps live in the `agents` table and are only changed by a human; agents cannot reach this module.
"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.db import get_pool
from core.settings import get_settings


class BudgetExceeded(Exception):
    pass


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


async def spent_today(role: str | None = None) -> Decimal:
    pool = await get_pool()
    async with pool.connection() as conn:
        if role is None:
            row = await (
                await conn.execute(
                    "SELECT COALESCE(SUM(spent_usd), 0) AS s FROM spend_daily WHERE day = %s", (today(),)
                )
            ).fetchone()
        else:
            row = await (
                await conn.execute(
                    "SELECT COALESCE(SUM(spent_usd), 0) AS s FROM spend_daily WHERE day = %s AND agent = %s",
                    (today(), role),
                )
            ).fetchone()
    return Decimal(row["s"])


async def check(role: str) -> Decimal:
    """Raise BudgetExceeded if the agent is paused, at its cap, or the team is at its ceiling.
    Returns what the agent may still spend today."""
    pool = await get_pool()
    async with pool.connection() as conn:
        agent = await (
            await conn.execute("SELECT enabled, daily_cap_usd FROM agents WHERE role = %s", (role,))
        ).fetchone()
    if agent is None or not agent["enabled"]:
        raise BudgetExceeded(f"agent {role} is disabled")
    mine = await spent_today(role)
    if mine >= agent["daily_cap_usd"]:
        raise BudgetExceeded(f"{role} spent ${mine:.2f} of its ${agent['daily_cap_usd']} daily cap")
    team = await spent_today()
    ceiling = Decimal(str(get_settings().team_daily_ceiling_usd))
    if team >= ceiling:
        raise BudgetExceeded(f"team spent ${team:.2f} of the ${ceiling} daily ceiling")
    return min(agent["daily_cap_usd"] - mine, ceiling - team)


async def record(role: str, cost_usd: float) -> tuple[Decimal, Decimal]:
    """Add a run's cost to today's total. Returns (spent today, cap)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                """INSERT INTO spend_daily (agent, day, spent_usd, cap_usd)
               SELECT %(role)s, %(day)s, %(cost)s, daily_cap_usd FROM agents WHERE role = %(role)s
               ON CONFLICT (agent, day) DO UPDATE SET spent_usd = spend_daily.spent_usd + EXCLUDED.spent_usd
               RETURNING spent_usd, cap_usd""",
                {"role": role, "day": today(), "cost": Decimal(str(round(cost_usd, 4)))},
            )
        ).fetchone()
    return Decimal(row["spent_usd"]), Decimal(row["cap_usd"])
