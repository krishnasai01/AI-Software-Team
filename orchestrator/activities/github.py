"""Light activities: claims, tickets, PRs, reviews, chat. Run on the orchestration queue.

Identities: the `orchestrator` App moves tickets (labels, comments, board); the engineer App opens
PRs; the reviewer App posts reviews. Each can only do its own part.
"""

from dataclasses import dataclass

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from core.chat import get_chat
from core.tickets import AGENT_READY, NEEDS_HUMAN, GitHubPermissionError, TicketRef, Tickets
from orchestrator import state
from orchestrator.types import AgentRunResult, NeedsHuman, PullRequest, ReviewInput, ReviewOutcome


@dataclass
class ClaimInput:
    repo: str
    number: int
    workflow_id: str
    agent: str = "engineer"


@dataclass
class OpenPrInput:
    repo: str
    number: int
    result: AgentRunResult


@dataclass
class TicketPrInput:
    repo: str
    number: int
    pr: PullRequest
    workflow_id: str


def _ref(repo: str, number: int) -> TicketRef:
    return TicketRef(repo, number)


async def _board(repo: str, number: int, status: str) -> None:
    """Mirror the state on the Projects board. Best effort: labels are the source of truth."""
    product = await state.product_for_repo(repo)
    if not product or not product.get("project_board_id"):
        return
    try:
        await Tickets("orchestrator").set_board_status(_ref(repo, number), product["project_board_id"], status)
    except Exception as e:  # noqa: BLE001
        activity.logger.warning("board update for %s#%s failed: %s", repo, number, e)


def _non_retryable(e: Exception) -> ApplicationError:
    return ApplicationError(str(e), type=type(e).__name__, non_retryable=True)


@activity.defn
async def claim_ticket(inp: ClaimInput) -> bool:
    ok = await state.claim(str(_ref(inp.repo, inp.number)), inp.agent, inp.workflow_id)
    if ok:
        await _board(inp.repo, inp.number, "In progress")
    return ok


@activity.defn
async def park_claim(inp: ClaimInput) -> None:
    await state.park(str(_ref(inp.repo, inp.number)), inp.workflow_id)


@activity.defn
async def release_claim(inp: ClaimInput) -> None:
    await state.release(str(_ref(inp.repo, inp.number)), inp.workflow_id)


@activity.defn
async def open_pr(inp: OpenPrInput) -> PullRequest:
    product = await state.product_for_repo(inp.repo)
    out = inp.result.output or {}
    t = _ref(inp.repo, inp.number)
    body = out.get("pr_body") or out.get("summary") or ""
    if f"#{inp.number}" not in body:
        body += f"\n\nCloses #{inp.number}"
    tests = "\n".join(f"- `{x['command']}`: {'pass' if x.get('passed') else 'FAIL'}" for x in out.get("tests_run", []))
    questions = "\n".join(f"- {q}" for q in out.get("open_questions", []))
    body += f"\n\n---\n**Tests run by the engineer**\n{tests or '- none reported'}"
    if questions:
        body += f"\n\n**Open questions**\n{questions}"
    body += f"\n\n<sub>Run `{inp.result.run_id}` · cost ${inp.result.cost_usd:.2f}</sub>"
    try:
        pr = await Tickets("engineer").open_or_update_pr(
            inp.repo, t.branch, product["default_branch"], out.get("pr_title") or f"Fix #{inp.number}", body
        )
    except GitHubPermissionError as e:
        raise _non_retryable(e) from e
    await _board(inp.repo, inp.number, "In review")
    # The engineer's push is the PR head; prefer the sha from the run over a possibly stale API view.
    return PullRequest(number=pr["number"], url=pr["html_url"], head_sha=inp.result.head_sha or pr["head"]["sha"])


@activity.defn
async def needs_human(inp: NeedsHuman) -> None:
    t = _ref(inp.repo, inp.number)
    tickets = Tickets("orchestrator")
    await tickets.add_labels(t, NEEDS_HUMAN)
    await tickets.comment(t, f"**Needs a human.** {inp.reason}\n\n<sub>workflow `{inp.workflow_id}`</sub>")
    await _board(inp.repo, inp.number, "Needs human")
    await get_chat().post(
        f":raising_hand: {t} needs you: {inp.reason}\nhttps://github.com/{inp.repo}/issues/{inp.number}"
    )
    await state.record_incident("needs_human", inp.reason[:500], agent="engineer", ticket=str(t))


@activity.defn
async def notify_merge_ready(inp: TicketPrInput) -> None:
    t = _ref(inp.repo, inp.number)
    link = await get_chat().post(
        f":white_check_mark: {t} is reviewed and ready for your merge: {inp.pr.url}\n"
        "Approve and merge on GitHub (branch protection requires your code-owner review)."
    )
    await state.create_approval("merge", inp.pr.url, inp.workflow_id, link or None)
    await _board(inp.repo, inp.number, "Awaiting merge")


@activity.defn
async def remind_merge(inp: TicketPrInput) -> None:
    await get_chat().post(f":hourglass: Reminder: {inp.pr.url} is still waiting for your merge.")


@activity.defn
async def finish_ticket(inp: TicketPrInput) -> None:
    """PR merged: close out the ticket's agent state."""
    t = _ref(inp.repo, inp.number)
    await state.decide_approval("merge", inp.pr.url, approved=True, by="github-merge")
    await Tickets("orchestrator").remove_label(t, AGENT_READY)
    await _board(inp.repo, inp.number, "Done")


@activity.defn
async def wait_for_ci(inp: ReviewInput) -> str:
    """Summarize CI for the PR head. Raises (and so is retried) while checks are still running."""
    state_, runs = await Tickets("orchestrator").checks_state(inp.repo, inp.head_sha)
    if state_ == "pending":
        raise ApplicationError("CI still running", type="CIPending")
    if state_ == "none":
        return "none: no CI checks are configured for this commit"
    failed = [r["name"] for r in runs if r["conclusion"] not in ("success", "neutral", "skipped")]
    return "success: all checks passed" if state_ == "success" else f"failure: {', '.join(failed)}"


@activity.defn
async def post_review(outcome: ReviewOutcome, repo: str) -> None:
    event = "APPROVE" if outcome.verdict == "approve" else "REQUEST_CHANGES"
    reviewer = Tickets("reviewer")
    try:
        await reviewer.post_review(repo, outcome.pr_number, outcome.head_sha, event, outcome.summary, outcome.comments)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 422 or not outcome.comments:
            raise
        # Inline comments on lines outside the diff are rejected; fold them into the body instead.
        body = (
            outcome.summary + "\n\n" + "\n".join(f"- `{c['path']}:{c['line']}`: {c['body']}" for c in outcome.comments)
        )
        await reviewer.post_review(repo, outcome.pr_number, outcome.head_sha, event, body)
    except GitHubPermissionError as e:
        raise _non_retryable(e) from e


ALL = [
    claim_ticket,
    park_claim,
    release_claim,
    open_pr,
    needs_human,
    notify_merge_ready,
    remind_merge,
    finish_ticket,
    wait_for_ci,
    post_review,
]
