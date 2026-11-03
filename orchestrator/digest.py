"""The morning digest: what shipped, what's in flight, spend, autonomy, and the short list that needs you."""

from core import budget
from orchestrator import state


async def build_digest() -> str:
    lines = ["*Agent team, daily digest*"]

    shipped = await state.q(
        "SELECT product, pr, ticket, auto FROM merges WHERE merged_at > now() - interval '1 day' ORDER BY merged_at"
    )
    lines.append(f"\n*Shipped* ({len(shipped)})")
    lines += [
        f"- {m['product']} #{m['pr']} (ticket #{m['ticket']}){' auto-merged' if m['auto'] else ''}" for m in shipped
    ]

    flight = await state.q(
        """SELECT t.product, t.number, t.status_note FROM tickets t
           WHERE t.updated_at > now() - interval '7 days' AND t.status_note IS NOT NULL
             AND t.status_note NOT LIKE 'merged%%' AND t.status_note <> 'stopped' ORDER BY t.product, t.number"""
    )
    lines.append(f"\n*In flight* ({len(flight)})")
    lines += [f"- {t['product']} #{t['number']}: {t['status_note']}" for t in flight[:30]]

    spent, top = await budget.spent_today(), await budget.ceiling()
    yday = await state.q1(
        "SELECT COALESCE(SUM(spent_usd), 0) AS s FROM spend_daily WHERE day = %s - 1", (budget.today(),)
    )
    per_pr = await state.q1(
        """SELECT COALESCE(SUM(r.cost_usd), 0) / NULLIF((SELECT COUNT(*) FROM merges
           WHERE merged_at > now() - interval '7 days'), 0) AS c
           FROM runs r WHERE r.started_at > now() - interval '7 days'"""
    )
    lines.append(
        f"\n*Spend*: ${spent:.2f} today, ${yday['s']:.2f} yesterday, ceiling ${top}/day"
        + (f"; ${per_pr['c']:.2f} per merged PR this week" if per_pr["c"] else "")
    )

    lines.append("\n*Autonomy*")
    for p in await state.enabled_products():
        a = await state.autonomy(p["name"])
        merges = await state.recent_merges(p["name"], days=30)
        auto = sum(1 for m in merges if m["auto"])
        bad = sum(1 for m in merges if m["outcome"] in ("broke_main", "reverted"))
        paused = " (auto-merge paused)" if a["auto_merge_paused"] else ""
        lines.append(
            f"- {p['name']}: level {a['level']}{paused}; 30 days: {len(merges)} merged, {auto} automatically, "
            f"{bad} reverted or broke main"
        )

    needs = []
    for a in await state.pending_approvals():
        if a["kind"] == "merge":
            needs.append(f"- Approve {a['target']}: {'; '.join(a['payload'].get('reasons', [])[:2])}")
        elif a["kind"] == "promotion":
            needs.append(
                f"- Promote {a['product']} to level {a['payload']['to']}: `orchestrator.admin approve {a['id']}`"
            )
        else:
            needs.append(f"- {a['kind']} {a['target']}: `orchestrator.admin approve {a['id']}`")
    for qn in await state.open_questions():
        needs.append(f"- Question {qn['id']} on {qn['product']} #{qn['ticket']}: {qn['question'][:200]}")
    pause = await state.pause_state()
    if pause:
        needs.append(f"- {pause}: `orchestrator.admin resume` when ready")
    for agent in await state.q("SELECT role FROM agents WHERE NOT enabled"):
        needs.append(f"- The {agent['role']} agent is paused: `orchestrator.admin enable {agent['role']}`")
    lines.append(f"\n*Needs you* ({len(needs)})")
    lines += needs or ["- Nothing today."]
    return "\n".join(lines)
