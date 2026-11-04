"""Carry out what `decide()` chose. Every action is safe to repeat: the next sweep re-reads state."""

import logging
from datetime import timedelta

from core.github import AGENT_READY, NEEDS_HUMAN, POLICY_CHECK
from orchestrator import jobs, state
from orchestrator.context import Ctx
from orchestrator.decide import (
    Action,
    ApplyDefault,
    CancelJobs,
    Enqueue,
    Escalate,
    EvaluatePolicy,
    RerunCI,
    Stop,
    TicketSnapshot,
    UpdateBranch,
    Wait,
)
from orchestrator.policy import (
    AUTO,
    HUMAN,
    POLICY_PATH,
    ChangedFile,
    Decision,
    PolicyError,
    PRFacts,
    evaluate,
    load_policy,
    risk_floor_only_rises,
)

log = logging.getLogger(__name__)

ANSWER_HELP = "Reply with a comment starting with `/answer`, for example `/answer 1` or `/answer use the EU rates`."


async def notify(ctx: Ctx, text: str) -> None:
    try:
        await ctx.chat.post(text)
    except Exception:  # noqa: BLE001 - chat is best effort; GitHub comments carry the record
        log.exception("chat post failed")


async def ask_human(
    ctx: Ctx,
    product: dict,
    number: int,
    question: str,
    options: list[str] | None = None,
    default: str | None = None,
    reversible: bool = False,
) -> None:
    """Park the ticket on a question. Everything else keeps going."""
    deadline = ctx.now() + timedelta(hours=ctx.settings.question_default_hours) if reversible and default else None
    qid = await state.create_question(product["name"], number, question, options or [], default, reversible, deadline)
    if qid is None:
        return  # already waiting on a question
    repo = product["repo"]
    lines = [f"**The agents need you** (question {qid}).", "", question]
    if options:
        lines += ["", *[f"{i}. {o}" for i, o in enumerate(options, 1)]]
    if deadline:
        lines += ["", f"If nobody answers by {deadline:%Y-%m-%d %H:%M} UTC, the agents go with: **{default}**."]
    lines += ["", ANSWER_HELP]
    await ctx.gh.add_labels(repo, number, NEEDS_HUMAN)
    await ctx.gh.comment(repo, number, "\n".join(lines))
    await notify(
        ctx, f":raising_hand: {repo}#{number} needs you: {question[:300]}\nhttps://github.com/{repo}/issues/{number}"
    )


async def record_answer(ctx: Ctx, product: dict, question: dict, status: str, answer: str, by: str) -> None:
    row = await state.resolve_question(question["id"], status, answer, by)
    if row is None:
        return
    await state.add_memory(
        product["name"],
        "answer",
        f"Q: {question['question']} A: {answer}",
        "human" if status == "answered" else "agent",
        question["ticket"],
    )
    repo, number = product["repo"], question["ticket"]
    await ctx.gh.remove_label(repo, number, NEEDS_HUMAN)
    note = "Thanks, the agents are continuing." if status == "answered" else f"No answer in time; going with: {answer}"
    await ctx.gh.comment(repo, number, note)


async def apply(ctx: Ctx, product: dict, snap: TicketSnapshot, action: Action) -> None:
    name, repo, number = product["name"], product["repo"], snap.number
    match action:
        case Wait(reason):
            await state.upsert_ticket(name, number, status_note=reason)
        case Enqueue(kind, mode, inp, head):
            pr = snap.pr.number if snap.pr and not (snap.pr.closed or snap.pr.merged) else None
            job_id = await jobs.enqueue(kind, mode, name, number, pr=pr, head_sha=head, input=inp)
            await state.upsert_ticket(
                name, number, status_note=f"queued {kind} {mode}" if job_id else "job already live"
            )
        case CancelJobs(reason):
            await jobs.cancel_live(name, number, reason)
        case Stop(reason):
            await jobs.cancel_live(name, number, reason)
            await ctx.gh.remove_label(repo, number, AGENT_READY)
            await ctx.gh.comment(repo, number, reason)
            await state.upsert_ticket(name, number, status_note="stopped")
        case UpdateBranch(head):
            try:
                await state.upsert_ticket(name, number, update_from_sha=head)
                await ctx.gh.update_branch(repo, snap.pr.number, head)
            except Exception as e:  # noqa: BLE001 - e.g. no longer behind; the next sweep re-reads
                log.info("update-branch on %s#%s: %s", repo, snap.pr.number, e)
        case RerunCI(head):
            for run_id in snap.pr.ci_run_ids:
                await ctx.gh.rerun_failed_jobs(repo, run_id)
            await state.upsert_ticket(name, number, ci_rerun_sha=head)
        case EvaluatePolicy():
            await evaluate_policy(ctx, product, snap)
        case Escalate(question, options, default, reversible):
            await ask_human(ctx, product, number, question, options, default, reversible)
        case ApplyDefault(question_id):
            row = await state.q1("SELECT * FROM questions WHERE id = %s", (question_id,))
            if row:
                await record_answer(ctx, product, row, "defaulted", row["default_answer"], "default")


async def pr_facts(ctx: Ctx, product: dict, snap: TicketSnapshot) -> PRFacts:
    pr = snap.pr
    files = []
    for f in await ctx.gh.pr_files(product["repo"], pr.number):
        files.append(ChangedFile(f["filename"], int(f.get("additions", 0)) + int(f.get("deletions", 0))))
        if f.get("previous_filename"):  # a rename out of a sensitive path counts as touching it
            files.append(ChangedFile(f["previous_filename"], 0))
    review_head = pr.review_head or pr.head_sha  # a clean update we made keeps the earlier review
    review = next(
        (j for j in snap.history if j.kind == "reviewer" and j.head_sha == review_head and j.status == "succeeded"),
        None,
    )
    ticket = await state.ticket(product["name"], snap.number) or {}
    return PRFacts(
        files=tuple(files),
        ci=pr.ci,
        external_origin=bool(ticket.get("external_origin")),
        reviewer_risk=risk_floor_only_rises((review.result if review else {}).get("risk")),
        authored_by_agent=True,  # agent/issue-N branches in this repo are only pushed by the worker
    )


async def evaluate_policy(ctx: Ctx, product: dict, snap: TicketSnapshot) -> Decision:
    """Decide who merges this head, post the required check, and act on it."""
    name, repo, pr = product["name"], product["repo"], snap.pr
    try:
        policy = load_policy(await ctx.gh.file_at(repo, POLICY_PATH, product["default_branch"]))
        autonomy = await state.autonomy(name)
        from orchestrator.sweeps import verify_config  # avoid an import cycle

        config_ok = await verify_config(ctx, product)
        decision = evaluate(
            policy, await pr_facts(ctx, product, snap), autonomy["level"], autonomy["auto_merge_paused"], config_ok
        )
    except PolicyError as e:
        decision = Decision(HUMAN, HUMAN, (str(e),))
    satisfied = decision.tier == AUTO or pr.human_approved
    await state.save_policy_decision(
        name,
        pr.number,
        pr.head_sha,
        decision.tier,
        decision.next_level_tier,
        (await state.autonomy(name))["level"],
        list(decision.reasons),
        satisfied,
    )

    reasons = "\n".join(f"- {r}" for r in decision.reasons)
    if satisfied:
        who = "you approved it" if decision.tier == HUMAN else "low risk under the merge policy"
        await ctx.gh.post_check(repo, pr.head_sha, POLICY_CHECK, "success", f"Merge allowed: {who}", reasons)
        try:
            await ctx.gh.enable_auto_merge(repo, pr.node_id)
        except Exception as e:  # noqa: BLE001
            await state.record_incident("auto_merge_failed", str(e)[:500], ticket=f"{repo}#{snap.number}")
            await notify(ctx, f":warning: Could not enable auto-merge on {pr.url} ({e}). Merge it yourself.")
        if pr.human_approved:
            await state.decide_approval_by_target("merge", pr.url, True, "github-approval")
        await state.upsert_ticket(name, snap.number, status_note="merge allowed; waiting for GitHub to merge")
        return decision

    await ctx.gh.post_check(repo, pr.head_sha, POLICY_CHECK, None, "Waiting for your approval", reasons)
    approval = await state.create_approval(
        "merge", name, pr.url, {"pr": pr.number, "head_sha": pr.head_sha, "reasons": list(decision.reasons)}, None
    )
    if approval:
        review = next(
            (j for j in snap.history if j.kind == "reviewer" and j.head_sha == (pr.review_head or pr.head_sha)), None
        )
        summary = (review.result.get("summary") if review else "") or ""
        brief = [
            "**Your approval is needed before this merges.** Why:",
            reasons,
            "",
            f"Reviewer's summary: {summary[:1500]}" if summary else "",
            "",
            "Approve this pull request on GitHub; the agents merge it after that.",
        ]
        await ctx.gh.comment(repo, pr.number, "\n".join(line for line in brief if line is not None))
        await notify(ctx, f":eyes: Approval needed for {pr.url}: {decision.reasons[0] if decision.reasons else ''}")
    await state.upsert_ticket(name, snap.number, status_note="waiting for your approval")
    return decision
