"""Product-wide checks that keep autonomy safe: the merge track record, watching main after merges,
automatic reverts and demotion, answers to questions, epics, outsider reviews, GitHub configuration
drift, the watchdog, and promotion proposals."""

import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.github import (
    AGENT_READY,
    AGENT_REVERT,
    AGENT_REVIEW,
    EPIC,
    POLICY_CHECK,
    SPLIT,
    issue_number_from_branch,
)
from orchestrator import jobs, state
from orchestrator.actions import notify, record_answer
from orchestrator.context import Ctx
from orchestrator.policy import AUTO

log = logging.getLogger(__name__)

CONFIG_TTL = timedelta(minutes=10)
PROMOTION_MIN_MERGES = 20
PROMOTION_QUIET_DAYS = 14
MAX_PROMOTED_LEVEL = 2  # level 3 (deploys) arrives with the DevOps agent
SPEND_SPIKE_FACTOR = 2
SPEND_SPIKE_MIN_USD = 5


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


# GitHub configuration: the human gate rests on it, so verify it and fail closed.


async def verify_config(ctx: Ctx, product: dict) -> bool:
    key = ("config", product["name"])
    cached = ctx.cache.get(key)
    if cached and ctx.now() - cached[1] < CONFIG_TTL:
        return cached[0]
    try:
        problems = config_problems(
            await ctx.gh.branch_rules(product["repo"], product["default_branch"]),
            ctx.settings.github_app_ids.get("orchestrator"),
        )
    except Exception as e:  # noqa: BLE001
        problems = [f"could not read the branch rules: {e}"]
    ok = not problems
    ctx.cache[key] = (ok, ctx.now())
    if not ok:
        alert_key = f"config_alert:{product['name']}"
        if await state.control(alert_key) != str(ctx.now().date()):
            await state.set_control(alert_key, str(ctx.now().date()), "reconciler")
            await notify(
                ctx,
                f":lock: {product['repo']} protection settings are incomplete, so nothing merges "
                "without you until they are fixed:\n" + "\n".join(f"- {p}" for p in problems),
            )
    return ok


def config_problems(rules: list[dict], orchestrator_app_id: int | None) -> list[str]:
    by_type: dict[str, dict] = {}
    for r in rules:
        by_type.setdefault(r["type"], r.get("parameters") or {})
    problems = []
    pr = by_type.get("pull_request")
    if pr is None:
        problems.append("no ruleset requires pull requests on the default branch")
    else:
        for flag, label in (
            ("require_code_owner_review", "require review from code owners"),
            ("dismiss_stale_reviews_on_push", "dismiss stale approvals"),
            ("require_last_push_approval", "require approval of the most recent push"),
        ):
            if not pr.get(flag):
                problems.append(f"the pull request rule must {label}")
    checks = by_type.get("required_status_checks")
    policy_check = [c for c in (checks or {}).get("required_status_checks", []) if c.get("context") == POLICY_CHECK]
    if not policy_check:
        problems.append(f"`{POLICY_CHECK}` is not a required status check")
    elif orchestrator_app_id and policy_check[0].get("integration_id") != orchestrator_app_id:
        problems.append(f"`{POLICY_CHECK}` must be pinned to the orchestrator App so nothing else can set it")
    if "merge_queue" not in by_type and not (checks or {}).get("strict_required_status_checks_policy"):
        problems.append("branches must be up to date before merging (or use a merge queue)")
    for t, label in (("non_fast_forward", "block force pushes"), ("deletion", "block deleting the branch")):
        if t not in by_type:
            problems.append(f"the ruleset must {label}")
    return problems


# Merges: close out tickets, watch main, revert and demote on a bad auto-merge.


async def sweep_merges(ctx: Ctx, product: dict) -> None:
    name, repo = product["name"], product["repo"]
    closed = await ctx.gh.recently_closed_prs(repo)
    for pr in closed:
        if not pr.get("merged_at"):
            continue
        reverted = re.search(rf"Reverts {re.escape(repo)}#(\d+)", pr.get("body") or "")
        if reverted:
            row = await state.merge_record(name, int(reverted.group(1)))
            if row and row["outcome"] != "reverted":
                await state.set_merge_outcome(name, row["pr"], "reverted", pr["number"])
                if row["auto"] and not row["revert_pr"]:
                    await demote(ctx, product, f"auto-merged PR #{row['pr']} was reverted by #{pr['number']}")
            continue
        if (pr.get("head", {}).get("repo") or {}).get("full_name") != repo:
            continue
        number = issue_number_from_branch(pr["head"]["ref"])
        if number is None or await state.merge_record(name, pr["number"]):
            continue
        decision = await state.policy_decision(name, pr["number"], pr["head"]["sha"])
        level = (await state.autonomy(name))["level"]
        auto = bool(decision and decision["tier"] == AUTO)
        await state.record_merge(
            name,
            pr["number"],
            number,
            pr.get("merge_commit_sha") or pr["head"]["sha"],
            auto,
            level,
            decision["next_level_tier"] if decision else None,
            _ts(pr["merged_at"]),
            timedelta(hours=ctx.settings.merge_watch_hours),
        )
        await state.decide_approval_by_target(
            "merge", pr["html_url"], True, (pr.get("merged_by") or {}).get("login", "github")
        )
        await ctx.gh.remove_label(repo, number, AGENT_READY)
        await state.upsert_ticket(name, number, status_note=f"merged in #{pr['number']}" + (" (auto)" if auto else ""))

    for row in await state.watched_merges(name):
        ci = await ctx.gh.ci_state(repo, row["merge_sha"])
        if ci.state == "failure":
            await bad_merge(
                ctx,
                product,
                row,
                f"CI failed on {product['default_branch']} after merging #{row['pr']}: {', '.join(ci.failed)}",
            )
        elif ci.state == "success" or ctx.now() > row["watch_until"]:
            await state.set_merge_outcome(name, row["pr"], "ok")


async def bad_merge(ctx: Ctx, product: dict, row: dict, detail: str) -> None:
    name, repo = product["name"], product["repo"]
    await state.set_merge_outcome(name, row["pr"], "broke_main")
    await state.record_incident("broke_main", detail[:500], ticket=f"{repo}#{row['ticket']}")
    if not row["auto"]:
        await notify(
            ctx, f":rotating_light: {detail}. You merged it, so the agents are not reverting it; decide what to do."
        )
        return
    pr = await ctx.gh.get_pr(repo, row["pr"])
    revert = await ctx.gh.revert_pr(
        repo,
        pr["node_id"],
        f"Revert #{row['pr']} (automatic)",
        f"{detail}\n\nReverted automatically because #{row['pr']} was auto-merged.",
    )
    await ctx.gh.add_labels(repo, revert, AGENT_REVERT)
    await state.set_merge_outcome(name, row["pr"], "broke_main", revert)
    await demote(ctx, product, detail)
    if row["ticket"]:
        await ctx.gh.set_issue_state(repo, row["ticket"], "open")
        await ctx.gh.add_labels(repo, row["ticket"], AGENT_READY)
        await ctx.gh.comment(
            repo,
            row["ticket"],
            f"Reopened: {detail}. The change is being reverted in #{revert}; "
            "the engineer will try again with this failure in mind.",
        )
        await state.add_memory(
            name, "lesson", f"#{row['pr']} broke {product['default_branch']}: {detail}", "agent", row["ticket"]
        )
    await notify(ctx, f":rotating_light: {detail}. Reverting in #{revert}; auto-merge paused and autonomy lowered.")


async def demote(ctx: Ctx, product: dict, reason: str) -> None:
    """Lowering autonomy is always automatic; raising it always needs you."""
    current = await state.autonomy(product["name"])
    level = max(0, current["level"] - 1)
    await state.set_autonomy(product["name"], level, True, "auto-demotion", reason[:500])
    await state.record_incident("demotion", f"level {current['level']} -> {level}: {reason}"[:500])
    await notify(
        ctx,
        f":arrow_down: {product['name']} autonomy lowered to level {level} and auto-merge paused: "
        f"{reason}. Resume with `orchestrator.admin resume-auto-merge {product['name']}`.",
    )


async def sweep_reverts(ctx: Ctx, product: dict, open_prs: list[dict]) -> None:
    """Our automatic reverts merge on their own once CI passes: they restore a known-good state."""
    name, repo = product["name"], product["repo"]
    for pr in open_prs:
        if AGENT_REVERT not in {lbl["name"] for lbl in pr.get("labels", [])}:
            continue
        head = pr["head"]["sha"]
        decision = await state.policy_decision(name, pr["number"], head)
        if decision and decision["satisfied"]:
            continue
        ci = await ctx.gh.ci_state(repo, head)
        if ci.state == "success":
            await state.save_policy_decision(
                name, pr["number"], head, AUTO, AUTO, 0, ["automatic revert of a bad auto-merge"], True
            )
            await ctx.gh.post_check(repo, head, POLICY_CHECK, "success", "Merge allowed: automatic revert", "")
            await ctx.gh.enable_auto_merge(repo, pr["node_id"])
        elif ci.state == "failure":
            await state.save_policy_decision(name, pr["number"], head, "human", "human", 0, ["revert failed CI"], False)
            await notify(ctx, f":rotating_light: The automatic revert {pr['html_url']} fails CI too. Needs you.")


# Questions, epics and outsider reviews


async def sweep_answers(ctx: Ctx, product: dict) -> None:
    for question in await state.open_questions(product["name"]):
        since = question["created_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for c in await ctx.gh.issue_comments(product["repo"], question["ticket"], since=since):
            body = (c.get("body") or "").strip()
            if c["user"]["login"] not in ctx.settings.human_approvers or not body.startswith("/answer"):
                continue
            answer = body.removeprefix("/answer").strip()
            options = question["options"] or []
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                answer = options[int(answer) - 1]
            await record_answer(ctx, product, question, "answered", answer, c["user"]["login"])
            break


async def sweep_epics(ctx: Ctx, product: dict) -> None:
    """Close an epic or split ticket once every sub-ticket is closed."""
    name, repo = product["name"], product["repo"]
    parents = {i["number"] for label in (EPIC, SPLIT) for i in await ctx.gh.list_open_issues(repo, label)}
    for parent in parents:
        kids = await state.children(name, parent)
        if kids and all((await ctx.gh.get_issue(repo, k))["state"] == "closed" for k in kids):
            await ctx.gh.comment(repo, parent, "All sub-tickets are done: " + ", ".join(f"#{k}" for k in kids))
            await ctx.gh.remove_label(repo, parent, AGENT_READY)
            await ctx.gh.set_issue_state(repo, parent, "closed")


async def sweep_outsider_reviews(ctx: Ctx, product: dict, open_prs: list[dict]) -> None:
    """PRs not written by the agents are reviewed only when a trusted person asks, with a label."""
    name, repo = product["name"], product["repo"]
    for pr in open_prs:
        if AGENT_REVIEW not in {lbl["name"] for lbl in pr.get("labels", [])}:
            continue
        head = pr["head"]["sha"]
        if await jobs.reviewed(name, pr["number"], head):
            continue
        if await ctx.gh.label_actor(repo, pr["number"], AGENT_REVIEW) not in ctx.settings.trusted_labelers:
            continue
        await jobs.enqueue("reviewer", "review", name, None, pr=pr["number"], head_sha=head, input={"outsider": True})


# Watchdog: agents pause themselves; only a human resumes them.


async def watchdog(ctx: Ctx) -> None:
    if await state.pause_state():
        return
    hour = await state.q1("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM runs WHERE ended_at > now() - interval '1 hour'")
    week = await state.q1("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM runs WHERE ended_at > now() - interval '7 days'")
    usual = Decimal(week["s"]) / (7 * 24)
    last_hour = Decimal(hour["s"])
    if last_hour > SPEND_SPIKE_MIN_USD and last_hour > SPEND_SPIKE_FACTOR * usual and usual > 0:
        reason = f"spend spike: ${last_hour:.2f} in the last hour vs ${usual:.2f}/hour usually"
        await state.pause(None, reason, "watchdog")
        await state.record_incident("spend_spike", reason)
        await notify(ctx, f":octagonal_sign: All agents paused ({reason}). Resume with `orchestrator.admin resume`.")

    for agent in await state.q("SELECT role FROM agents WHERE enabled"):
        last = await state.q(
            "SELECT outcome FROM runs WHERE agent = %s AND ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 3",
            (agent["role"],),
        )
        if len(last) == 3 and all(r["outcome"] in ("error", "timeout", "runner_error") for r in last):
            await state.q("UPDATE agents SET enabled = false WHERE role = %s", (agent["role"],))
            await state.record_incident("agent_disabled", "three failed runs in a row", agent=agent["role"])
            await notify(
                ctx,
                f":octagonal_sign: The {agent['role']} agent failed three runs in a row and is paused. "
                f"Check the runs table, then `orchestrator.admin enable {agent['role']}`.",
            )


# Promotion: proposed from the track record, applied only with your approval.


async def propose_promotions(ctx: Ctx, product: dict) -> None:
    name = product["name"]
    auto = await state.autonomy(name)
    level = auto["level"]
    if level >= MAX_PROMOTED_LEVEL or auto["auto_merge_paused"]:
        return
    if ctx.now() - auto["changed_at"] < timedelta(days=PROMOTION_QUIET_DAYS):
        return
    since = [m for m in await state.recent_merges(name, days=365) if m["merged_at"] >= auto["changed_at"]]
    if any(m["outcome"] in ("broke_main", "reverted") for m in since):
        return
    if level == 0:
        good = [m for m in since if m["next_level_tier"] == AUTO and m["outcome"] == "ok"]
    else:
        good = [m for m in since if m["auto"] and m["outcome"] == "ok"]
    if len(good) < PROMOTION_MIN_MERGES:
        return
    approval = await state.create_approval(
        "promotion", name, f"{name}:level-{level + 1}", {"from": level, "to": level + 1, "evidence": len(good)}, None
    )
    if approval:
        await notify(
            ctx,
            f":arrow_up: {name} has {len(good)} clean merges at autonomy level {level} and "
            f"{PROMOTION_QUIET_DAYS} quiet days. Promote to level {level + 1}? "
            f"`orchestrator.admin approve {approval}`",
        )
