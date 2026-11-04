"""Operator commands. Everything that expands what the agents may do lives here, with a human.

python -m orchestrator.admin status
python -m orchestrator.admin add-product <name> <owner/repo> <sandbox-image> [--branch main]
python -m orchestrator.admin pause [--product NAME] [--reason TEXT]     # kill switch
python -m orchestrator.admin resume [--product NAME]
python -m orchestrator.admin approve <approval-id> | reject <approval-id>
python -m orchestrator.admin answer <question-id> <text>
python -m orchestrator.admin resume-auto-merge <product>
python -m orchestrator.admin set-level <product> <0-3>
python -m orchestrator.admin set-cap <role> <usd> | set-ceiling <usd>
python -m orchestrator.admin enable <role> | disable <role>
"""

import argparse
import asyncio
import getpass

from core.db import close_pool
from orchestrator import state


async def run(args: argparse.Namespace) -> str:
    me = f"human:{getpass.getuser()}"
    match args.cmd:
        case "status":
            lines = [f"pause: {await state.pause_state() or 'no'}"]
            for p in await state.enabled_products():
                a = await state.autonomy(p["name"])
                lines.append(
                    f"{p['name']} ({p['repo']}): level {a['level']}"
                    + (", auto-merge paused" if a["auto_merge_paused"] else "")
                    + (f", {await state.pause_state(p['name'])}" if await state.pause_state(p["name"]) else "")
                )
            for a in await state.pending_approvals():
                lines.append(f"approval {a['id']}: {a['kind']} {a['target']}")
            for qn in await state.open_questions():
                lines.append(f"question {qn['id']} ({qn['product']} #{qn['ticket']}): {qn['question'][:120]}")
            return "\n".join(lines)
        case "add-product":
            await state.q(
                """INSERT INTO products (name, repo, sandbox_image, default_branch) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET repo = EXCLUDED.repo, sandbox_image = EXCLUDED.sandbox_image,
                     default_branch = EXCLUDED.default_branch""",
                (args.name, args.repo, args.image, args.branch),
            )
            await state.autonomy(args.name)
            return f"{args.name} added at autonomy level 0 (shadow)"
        case "pause":
            await state.pause(args.product, args.reason, me)
            return f"paused {args.product or 'all agents'}"
        case "resume":
            await state.resume(args.product, me)
            return f"resumed {args.product or 'all agents'}"
        case "approve" | "reject":
            row = await state.decide_approval(args.id, args.cmd == "approve", me)
            if row is None:
                return f"no pending approval {args.id}"
            if row["kind"] == "promotion" and args.cmd == "approve":
                level = row["payload"]["to"]
                await state.set_autonomy(row["product"], level, False, me, "promotion approved")
                return f"{row['product']} promoted to level {level}"
            return f"{args.cmd}d {row['kind']} {row['target']}"
        case "answer":
            qn = await state.q1("SELECT * FROM questions WHERE id = %s AND status = 'open'", (args.id,))
            if qn is None:
                return f"no open question {args.id}"
            from orchestrator.actions import record_answer
            from orchestrator.context import default_ctx

            await record_answer(default_ctx(), await state.product(qn["product"]), qn, "answered", args.text, me)
            return "answered"
        case "resume-auto-merge":
            a = await state.autonomy(args.product)
            await state.set_autonomy(args.product, a["level"], False, me, "auto-merge resumed")
            return f"auto-merge resumed for {args.product} at level {a['level']}"
        case "set-level":
            await state.set_autonomy(args.product, args.level, False, me, "set by hand")
            return f"{args.product} set to level {args.level}"
        case "set-cap":
            await state.q("UPDATE agents SET daily_cap_usd = %s WHERE role = %s", (args.usd, args.role))
            return f"{args.role} cap set to ${args.usd}/day"
        case "set-ceiling":
            await state.set_control("ceiling_usd", args.usd, me)
            return f"team ceiling set to ${args.usd}/day"
        case "enable" | "disable":
            await state.q("UPDATE agents SET enabled = %s WHERE role = %s", (args.cmd == "enable", args.role))
            return f"{args.cmd}d {args.role}"
    raise SystemExit(f"unknown command {args.cmd}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("add-product")
    a.add_argument("name")
    a.add_argument("repo")
    a.add_argument("image")
    a.add_argument("--branch", default="main")
    for name in ("pause", "resume"):
        c = sub.add_parser(name)
        c.add_argument("--product")
        if name == "pause":
            c.add_argument("--reason", default="paused by hand")
    for name in ("approve", "reject"):
        sub.add_parser(name).add_argument("id", type=int)
    c = sub.add_parser("answer")
    c.add_argument("id", type=int)
    c.add_argument("text")
    sub.add_parser("resume-auto-merge").add_argument("product")
    c = sub.add_parser("set-level")
    c.add_argument("product")
    c.add_argument("level", type=int, choices=range(4))
    c = sub.add_parser("set-cap")
    c.add_argument("role")
    c.add_argument("usd", type=float)
    sub.add_parser("set-ceiling").add_argument("usd", type=float)
    for name in ("enable", "disable"):
        sub.add_parser(name).add_argument("role")
    return p


async def main() -> None:
    try:
        print(await run(parser().parse_args()))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
