"""Spend: per-role daily caps under a team-wide ceiling.

LiteLLM enforces the caps on every request (one team per role), so concurrent runs cannot
overshoot and a crashed worker cannot lose spend. This module decides whether to *start* a run,
keeps the per-day reporting copy, and is the budget broker: when a role runs out while the team is
well under its ceiling, it may borrow up to half its cap again. Only a human raises the ceiling.
"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.db import get_pool
from core.settings import get_settings

BORROW_FACTOR = Decimal("1.5")  # a role may reach 150% of its cap...
BORROW_WHILE_TEAM_UNDER = Decimal("0.7")  # ...while the team is under 70% of the ceiling


class BudgetExceeded(Exception):
    pass


def today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


async def _one(sql: str, params: tuple | dict = ()) -> dict | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        return await (await conn.execute(sql, params)).fetchone()


async def ceiling() -> Decimal:
    row = await _one("SELECT value FROM controls WHERE key = 'ceiling_usd'")
    return Decimal(str(row["value"] if row else get_settings().team_daily_ceiling_usd))


async def spent_today(role: str | None = None) -> Decimal:
    if role is None:
        row = await _one("SELECT COALESCE(SUM(spent_usd), 0) AS s FROM spend_daily WHERE day = %s", (today(),))
    else:
        row = await _one(
            "SELECT COALESCE(SUM(spent_usd), 0) AS s FROM spend_daily WHERE day = %s AND agent = %s", (today(), role)
        )
    return Decimal(row["s"])


async def effective_cap(role: str) -> Decimal:
    """The role's cap for today, including what the broker lets it borrow."""
    agent = await _one("SELECT daily_cap_usd FROM agents WHERE role = %s", (role,))
    if agent is None:
        raise BudgetExceeded(f"unknown agent role {role}")
    cap = Decimal(agent["daily_cap_usd"])
    if await spent_today() < await ceiling() * BORROW_WHILE_TEAM_UNDER:
        return cap * BORROW_FACTOR
    return cap


async def check(role: str) -> Decimal:
    """Raise BudgetExceeded if the role may not start a run now. Returns what it may still spend."""
    agent = await _one("SELECT enabled FROM agents WHERE role = %s", (role,))
    if agent is None or not agent["enabled"]:
        raise BudgetExceeded(f"agent {role} is disabled")
    team, top = await spent_today(), await ceiling()
    if team >= top:
        raise BudgetExceeded(f"team spent ${team:.2f} of the ${top} daily ceiling")
    mine, cap = await spent_today(role), await effective_cap(role)
    if mine >= cap:
        raise BudgetExceeded(f"{role} spent ${mine:.2f} of its ${cap:.2f} cap for today")
    return min(cap - mine, top - team)


async def record(role: str, cost_usd: float) -> tuple[Decimal, Decimal]:
    """Add a run's cost to today's reporting copy. Returns (spent today, base cap)."""
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


async def sync_proxy_teams() -> None:
    """Make LiteLLM's per-role team budgets match today's effective caps."""
    from core.model import LiteLLM

    proxy = LiteLLM()
    pool = await get_pool()
    async with pool.connection() as conn:
        roles = await (await conn.execute("SELECT role, litellm_team_id FROM agents ORDER BY role")).fetchall()
    for r in roles:
        team_id = await proxy.ensure_team(r["litellm_team_id"], r["role"], float(await effective_cap(r["role"])))
        if team_id != r["litellm_team_id"]:
            async with pool.connection() as conn:
                await conn.execute("UPDATE agents SET litellm_team_id = %s WHERE role = %s", (team_id, r["role"]))
