"""Build the snapshot `decide()` reads: current GitHub state plus our own job history."""

from datetime import datetime

from core.github import AGENT_READY, NEEDS_BREAKDOWN, agent_branch
from orchestrator import jobs, state
from orchestrator.context import Ctx
from orchestrator.decide import JobView, PolicyView, PRView, QuestionView, TicketSnapshot

TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
CREATED_BY_US = "agent-company"  # labeled_by marker for tickets the tech lead split out
_LOOKUP = object()


def _job_view(j: jobs.Job) -> JobView:
    result = dict(j.result or {})
    if j.error and "error" not in result:
        result["error"] = j.error
    return JobView(j.id, j.kind, j.mode, j.status, j.outcome, result, j.head_sha, j.created_at)


def _question_view(row: dict | None) -> QuestionView | None:
    if row is None:
        return None
    return QuestionView(
        row["id"],
        row["status"],
        row["answer"],
        row["reversible"],
        row["default_answer"],
        row["deadline"],
        row["answered_at"],
    )


async def labeler_trusted(ctx: Ctx, product: dict, issue: dict, labels: frozenset[str]) -> bool:
    """Was agent-ready (or needs-breakdown) applied by someone on the trusted list?"""
    name, number = product["name"], issue["number"]
    row = await state.ticket(name, number)
    label = AGENT_READY if AGENT_READY in labels else NEEDS_BREAKDOWN if NEEDS_BREAKDOWN in labels else None
    if label is None:
        if row and row["labeled_by"] and row["labeled_by"] != CREATED_BY_US:
            await state.upsert_ticket(name, number, labeled_by=None)  # re-check if it is labeled again
        return True
    if row and row["labeled_by"]:
        labeled_by = row["labeled_by"]
    else:
        labeled_by = await ctx.gh.label_actor(product["repo"], number, label)
        external = issue.get("author_association") not in TRUSTED_ASSOCIATIONS
        if row and row["external_origin"]:
            external = True  # inherited from a parent ticket
        await state.upsert_ticket(name, number, labeled_by=labeled_by, external_origin=external)
    return labeled_by == CREATED_BY_US or labeled_by in ctx.settings.trusted_labelers


async def pr_view(ctx: Ctx, product: dict, number: int, pr: dict, history: list[jobs.Job]) -> PRView | None:
    repo, name = product["repo"], product["name"]
    if (pr.get("head", {}).get("repo") or {}).get("full_name") != repo:
        return None  # a fork's branch can be named anything; it never belongs to a ticket
    if pr.get("merged_at") or pr.get("state") == "closed":
        return PRView(
            pr["number"],
            pr["head"]["sha"],
            bool(pr.get("merged_at")),
            True,
            False,
            "unknown",
            "none",
            (),
            (),
            False,
            0,
            None,
            0,
            False,
            None,
            pr.get("node_id", ""),
            pr.get("html_url", ""),
        )
    full = await ctx.gh.get_pr(repo, pr["number"])
    head = full["head"]["sha"]
    row = await state.ticket(name, number) or {}
    now = ctx.now()
    seen: datetime = row.get("head_seen_at") if row.get("head_sha") == head else None
    if seen is None:
        seen = now
        await state.upsert_ticket(name, number, head_sha=head, head_seen_at=now)
    ci = await ctx.gh.ci_state(repo, head)
    ours = [j for j in history if j.kind == "reviewer" and j.pr == pr["number"] and j.status == "succeeded"]
    review = next((j.result.get("verdict") for j in ours if j.head_sha == head), None)
    review_head = head if review else None
    previous = row.get("update_from_sha")
    if review is None and previous and previous != head:
        # A branch update we requested is a merge of the base into the reviewed head. If that is
        # exactly what the new head is, the change under review is the same: keep the verdict.
        carried = next((j.result.get("verdict") for j in ours if j.head_sha == previous), None)
        if carried and (await ctx.gh.commit_parents(repo, head))[:1] == [previous]:
            review, review_head = carried, previous
    rounds = sum(1 for j in ours if j.result.get("verdict") == "request_changes")
    reviews = await ctx.gh.pr_reviews(repo, pr["number"])
    human_approved = any(
        r["user"]["login"] in ctx.settings.human_approvers and r["state"] == "APPROVED" and r.get("commit_id") == head
        for r in reviews
    )
    pd = await state.policy_decision(name, pr["number"], head)
    return PRView(
        number=pr["number"],
        head_sha=head,
        merged=False,
        closed=False,
        draft=bool(full.get("draft")),
        mergeable_state=full.get("mergeable_state") or "unknown",
        ci=ci.state,
        ci_failed=ci.failed,
        ci_run_ids=ci.failed_run_ids,
        ci_rerun_done=row.get("ci_rerun_sha") == head,
        head_age_minutes=(now - seen).total_seconds() / 60,
        review=review,
        review_rounds=rounds,
        human_approved=human_approved,
        policy=PolicyView(pd["tier"], pd["satisfied"]) if pd else None,
        node_id=full.get("node_id", ""),
        url=full.get("html_url", ""),
        review_head=review_head,
    )


async def build(
    ctx: Ctx, product: dict, number: int, issue: dict | None = None, open_pr=_LOOKUP, paused: str | None = None
) -> TicketSnapshot:
    repo, name = product["repo"], product["name"]
    issue = issue or await ctx.gh.get_issue(repo, number)
    labels = frozenset(lbl["name"] for lbl in issue.get("labels", []))
    trusted = await labeler_trusted(ctx, product, issue, labels)
    live = await jobs.live(name, number)
    history = await jobs.history(name, number)

    pr_data = await ctx.gh.find_open_pr(repo, agent_branch(number)) if open_pr is _LOOKUP else open_pr
    if pr_data is None and any(j.kind == "engineer" and j.outcome == "completed" for j in history):
        owner = repo.split("/", 1)[0]
        closed = await ctx.gh.list_prs(repo, state="closed", head=f"{owner}:{agent_branch(number)}")
        pr_data = max(closed, key=lambda p: p["number"]) if closed else None
    pr = await pr_view(ctx, product, number, pr_data, history) if pr_data else None

    return TicketSnapshot(
        product=name,
        number=number,
        issue_open=issue.get("state") == "open",
        labels=labels,
        labeler_trusted=trusted,
        paused=paused or await state.pause_state(name),
        live_job=f"{live.kind} {live.mode} {live.status}" if live else None,
        history=[_job_view(j) for j in history],
        question=_question_view(await state.latest_question(name, number)),
        pr=pr,
        now=ctx.now(),
        max_jobs=ctx.settings.max_jobs_per_ticket,
        ci_grace_minutes=ctx.settings.ci_grace_minutes,
        ci_stuck_minutes=ctx.settings.ci_stuck_hours * 60,
    )
