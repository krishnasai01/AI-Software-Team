"""Workflow and activity payloads, plus the names shared by workflows, workers and the router."""

from dataclasses import dataclass, field

# Task queues: light orchestration work runs anywhere; agent runs only where sandboxes can start.
ORCHESTRATION_QUEUE = "orchestration"
AGENT_QUEUE = "agent-runs"

MAX_REVIEW_ROUNDS = 3


def ticket_workflow_id(repo: str, number: int) -> str:
    return f"ticket-{repo.replace('/', '-')}-{number}"


def review_workflow_id(repo: str, pr_number: int, head_sha: str) -> str:
    return f"review-{repo.replace('/', '-')}-{pr_number}-{head_sha[:12]}"


@dataclass
class TicketInput:
    repo: str
    number: int


@dataclass
class AgentRunInput:
    role: str
    repo: str
    ticket_number: int
    workflow_id: str
    feedback: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    ci_summary: str | None = None


@dataclass
class AgentRunResult:
    run_id: str
    outcome: str  # completed, blocked, error, timeout, error_max_turns, error_max_budget_usd, ...
    output: dict | None = None
    head_sha: str | None = None
    pushed: bool = False
    cost_usd: float = 0.0
    note: str = ""


@dataclass
class PullRequest:
    number: int
    url: str
    head_sha: str


@dataclass
class ReviewInput:
    repo: str
    pr_number: int
    head_sha: str
    ticket_number: int | None = None


@dataclass
class ReviewOutcome:
    verdict: str  # approve, request_changes, error
    summary: str
    pr_number: int
    head_sha: str
    comments: list[dict] = field(default_factory=list)


@dataclass
class NeedsHuman:
    repo: str
    number: int
    reason: str
    workflow_id: str
