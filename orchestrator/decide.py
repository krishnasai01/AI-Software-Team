"""`decide(snapshot) -> action`: the whole ticket lifecycle as one pure function.

The reconciler builds a snapshot of a ticket from GitHub (the source of truth) and the job
history, and applies whatever this returns. Because it reads current state each time instead of
reacting to events, a missed, late, duplicated or forged event cannot leave a ticket in a wrong
state: the next look simply sees the truth. Rules are checked in order; the first match wins.
"""

from dataclasses import dataclass, field
from datetime import datetime

from core.github import AGENT_READY, EPIC, NEEDS_BREAKDOWN, SPLIT

MAX_REVIEW_ROUNDS = 3
MAX_CLARIFICATIONS = 2
MAX_ARBITRATIONS = 2


@dataclass
class JobView:
    id: int
    kind: str
    mode: str
    status: str  # succeeded, failed
    outcome: str | None  # completed, blocked, cap, rejected, no_changes, error, ... (from the result)
    result: dict
    head_sha: str | None
    created_at: datetime


@dataclass
class QuestionView:
    id: int
    status: str  # open, answered, defaulted, cancelled
    answer: str | None
    reversible: bool
    default_answer: str | None
    deadline: datetime | None
    answered_at: datetime | None


@dataclass
class PolicyView:
    tier: str
    satisfied: bool


@dataclass
class PRView:
    number: int
    head_sha: str
    merged: bool
    closed: bool
    draft: bool
    mergeable_state: str  # clean, behind, dirty, blocked, unstable, unknown
    ci: str  # pending, success, failure, none
    ci_failed: tuple[str, ...]
    ci_run_ids: tuple[int, ...]  # failed GitHub Actions runs that can be re-run
    ci_rerun_done: bool
    head_age_minutes: float  # since we first saw this head
    review: str | None  # our reviewer's verdict on this head: approve, request_changes
    review_rounds: int  # times our reviewer requested changes on this PR
    human_approved: bool  # a human approver approved this head
    policy: PolicyView | None
    node_id: str = ""
    url: str = ""
    review_head: str | None = None  # the head whose review counts (differs only after our clean update)


@dataclass
class TicketSnapshot:
    product: str
    number: int
    issue_open: bool
    labels: frozenset[str]
    labeler_trusted: bool
    paused: str | None  # reason, or None
    live_job: str | None  # "kind mode status" if a job is queued or running
    history: list[JobView]  # finished jobs, newest first
    question: QuestionView | None  # the latest question for this ticket
    pr: PRView | None
    now: datetime
    max_jobs: int = 12
    ci_grace_minutes: int = 10
    ci_stuck_minutes: int = 180


# Actions


@dataclass
class Wait:
    reason: str


@dataclass
class Enqueue:
    kind: str
    mode: str
    input: dict = field(default_factory=dict)
    head_sha: str | None = None


@dataclass
class CancelJobs:
    reason: str


@dataclass
class Stop:
    reason: str  # posted on the issue; agent-ready is removed


@dataclass
class UpdateBranch:
    head_sha: str


@dataclass
class RerunCI:
    head_sha: str


@dataclass
class EvaluatePolicy:
    head_sha: str


@dataclass
class Escalate:
    question: str
    options: list[str] = field(default_factory=list)
    default: str | None = None
    reversible: bool = False


@dataclass
class ApplyDefault:
    question_id: int


Action = Wait | Enqueue | CancelJobs | Stop | UpdateBranch | RerunCI | EvaluatePolicy | Escalate | ApplyDefault


def decide(s: TicketSnapshot) -> Action:
    if not s.issue_open or (AGENT_READY not in s.labels and NEEDS_BREAKDOWN not in s.labels):
        return (
            CancelJobs("the ticket is closed or no longer agent-ready") if s.live_job else Wait("not an active ticket")
        )
    if s.labels & {SPLIT, EPIC} and NEEDS_BREAKDOWN not in s.labels:
        return Wait("split into sub-tickets; closes when they are done")
    if not s.labeler_trusted:
        return Stop("Only people on the trusted list can hand work to the agents; removing `agent-ready`.")
    if s.paused:
        return Wait(s.paused)
    if s.live_job:
        return Wait(s.live_job)

    q = s.question
    if q and q.status == "open":
        if q.reversible and q.default_answer and q.deadline and s.now >= q.deadline:
            return ApplyDefault(q.id)
        return Wait("waiting for your answer")
    answered = q if q and q.status in ("answered", "defaulted") else None
    since_answer = [j for j in s.history if not answered or j.created_at > answered.answered_at]
    if len(since_answer) >= s.max_jobs:
        return Escalate(
            f"This ticket has used {len(since_answer)} agent runs without finishing. "
            "Clarify it, split it, or remove `agent-ready`."
        )

    if NEEDS_BREAKDOWN in s.labels:
        return _epic(s)

    # A fresh answer from you is acted on before anything else.
    if answered and not any(j.created_at > answered.answered_at for j in s.history):
        open_pr = s.pr if s.pr and not (s.pr.closed or s.pr.merged) else None
        return Enqueue(
            "engineer",
            "rework" if open_pr else "implement",
            {"guidance": f"The owner answered: {answered.answer}"},
            head_sha=open_pr.head_sha if open_pr else None,
        )

    pr = s.pr
    if pr and pr.merged:
        return Wait("merged; the merge sweep closes out the ticket")
    if pr and pr.closed:
        return Stop(
            "The pull request was closed without merging, so the agents stopped. Add `agent-ready` again to restart."
        )
    if pr is None:
        return _no_pr(s)
    return _open_pr(s, pr)


def _count(history: list[JobView], kind: str, mode: str | None = None, head: str | None = None) -> int:
    return sum(
        1
        for j in history
        if j.kind == kind and (mode is None or j.mode == mode) and (head is None or j.head_sha == head)
    )


def _epic(s: TicketSnapshot) -> Action:
    last = next((j for j in s.history if j.kind == "techlead" and j.mode == "breakdown"), None)
    if last is None:
        return Enqueue("techlead", "breakdown")
    if last.status == "failed":
        return Escalate("The tech lead could not break this epic down. Split it by hand or clarify it.")
    return Wait("broken down; waiting for the labels to settle")


def _ladder(s: TicketSnapshot, pr: PRView | None) -> Action | None:
    """The escalation ladder, applied to the last thing that happened on the current head."""
    last = s.history[0] if s.history else None
    head = pr.head_sha if pr else None
    if last is None or (pr is not None and last.head_sha != head):
        return None
    mode = "rework" if pr else "implement"

    if last.kind == "techlead" and last.mode in ("clarify", "split"):
        if last.status == "failed":
            return Escalate(f"The tech lead's {last.mode} run failed: {last.result.get('error', 'unknown error')}")
        d = last.result.get("decision")
        if d == "split":
            return Wait("split into sub-tickets")
        if d == "ask_human":
            return Escalate(last.result.get("question", {}).get("question") or "The tech lead needs your input.")
        return Enqueue("engineer", mode, {"guidance": last.result.get("guidance", "")}, head_sha=head)

    if last.kind != "engineer":
        return None
    o = last.outcome
    if last.status == "failed" or o in ("cap", "timeout"):
        why = last.result.get("error") or o or "it kept failing"
        if _count(s.history, "techlead", "split"):
            return Escalate(f"The engineer still can't finish this ticket after the tech lead re-scoped it ({why}).")
        return Enqueue("techlead", "split", {"reason": f"The engineer could not finish: {why}"}, head_sha=head)
    if o == "rejected":
        return Escalate(
            "The engineer's change needs protected paths, which agents may not change: "
            f"{last.result.get('reason', '')}. Make that part yourself, or tell the agents how to avoid it."
        )
    if o in ("blocked", "no_changes"):
        if _count(s.history, "techlead", "clarify") >= MAX_CLARIFICATIONS:
            questions = "; ".join(last.result.get("questions") or []) or last.result.get("summary", "")
            return Escalate(f"The engineer is still blocked after the tech lead's help: {questions}")
        return Enqueue(
            "techlead",
            "clarify",
            {
                "questions": last.result.get("questions") or ["The engineer finished without changing anything."],
                "engineer_summary": last.result.get("summary", ""),
            },
            head_sha=head,
        )
    return None


def _no_pr(s: TicketSnapshot) -> Action:
    if not s.history:
        return Enqueue("engineer", "implement")
    action = _ladder(s, None)
    if action:
        return action
    if s.history[0].kind == "engineer" and s.history[0].outcome == "completed":
        return Wait("waiting for the pull request to appear")
    return Enqueue("engineer", "implement")


def _open_pr(s: TicketSnapshot, pr: PRView) -> Action:
    if pr.draft:
        return Wait("draft pull request")
    action = _ladder(s, pr)
    if action:
        return action
    if pr.mergeable_state == "dirty":
        return Enqueue("engineer", "update", head_sha=pr.head_sha)
    if pr.mergeable_state == "behind":
        return UpdateBranch(pr.head_sha)

    if pr.ci == "pending" or (pr.ci == "none" and pr.head_age_minutes < s.ci_grace_minutes):
        if pr.ci == "pending" and pr.head_age_minutes > s.ci_stuck_minutes:
            return Escalate(f"CI on PR #{pr.number} has been running for over {s.ci_stuck_minutes // 60} hours.")
        return Wait("waiting for CI")
    if pr.ci == "failure":
        if pr.ci_run_ids and not pr.ci_rerun_done:
            return RerunCI(pr.head_sha)
        return Enqueue("engineer", "fix-ci", {"ci_failures": list(pr.ci_failed)}, head_sha=pr.head_sha)

    review_jobs = [j for j in s.history if j.kind == "reviewer" and j.head_sha == pr.head_sha]
    if pr.review is None:
        if review_jobs and review_jobs[0].status == "failed":
            return Escalate(f"The reviewer could not review PR #{pr.number}: {review_jobs[0].result.get('error', '')}")
        return Enqueue("reviewer", "review", head_sha=pr.head_sha)

    approved = pr.review == "approve"
    if pr.review == "request_changes":
        ruling = next(
            (
                j
                for j in s.history
                if j.kind == "techlead"
                and j.mode == "arbitrate"
                and j.head_sha == pr.head_sha
                and j.status == "succeeded"
            ),
            None,
        )
        if ruling and ruling.result.get("decision") == "side_with_engineer":
            approved = True
        elif ruling and ruling.result.get("decision") == "side_with_reviewer":
            return Enqueue("engineer", "rework", {"guidance": ruling.result.get("guidance", "")}, head_sha=pr.head_sha)
        elif ruling and ruling.result.get("decision") == "ask_human":
            return Escalate(ruling.result.get("question", {}).get("question") or "The tech lead needs your ruling.")
        elif pr.review_rounds >= MAX_REVIEW_ROUNDS:
            if _count(s.history, "techlead", "arbitrate") >= MAX_ARBITRATIONS:
                return Escalate(f"The engineer and reviewer still disagree on PR #{pr.number} after arbitration.")
            return Enqueue("techlead", "arbitrate", head_sha=pr.head_sha)
        else:
            return Enqueue("engineer", "rework", head_sha=pr.head_sha)

    if approved:
        p = pr.policy
        if p is None or (p.tier == "human" and pr.human_approved and not p.satisfied):
            return EvaluatePolicy(pr.head_sha)
        if p.satisfied:
            return Wait("approved; GitHub merges when the required checks pass")
        return Wait("waiting for your approval")
    return Wait("nothing to do")
