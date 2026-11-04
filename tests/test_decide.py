"""decide() is the whole ticket lifecycle; these are table tests over snapshots."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.decide import (
    ApplyDefault,
    CancelJobs,
    Enqueue,
    Escalate,
    EvaluatePolicy,
    JobView,
    PolicyView,
    PRView,
    QuestionView,
    RerunCI,
    Stop,
    TicketSnapshot,
    UpdateBranch,
    Wait,
    decide,
)

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
_ids = iter(range(1, 10_000))


def job(kind, mode="implement", status="succeeded", outcome="completed", head=None, minutes_ago=10, **result):
    return JobView(
        next(_ids),
        kind,
        mode,
        status,
        outcome,
        {"outcome": outcome, **result},
        head,
        NOW - timedelta(minutes=minutes_ago),
    )


def pr(**kw) -> PRView:
    base = dict(
        number=7,
        head_sha="h1",
        merged=False,
        closed=False,
        draft=False,
        mergeable_state="clean",
        ci="success",
        ci_failed=(),
        ci_run_ids=(),
        ci_rerun_done=False,
        head_age_minutes=30,
        review=None,
        review_rounds=0,
        human_approved=False,
        policy=None,
    )
    return PRView(**{**base, **kw})


def snap(**kw) -> TicketSnapshot:
    base = dict(
        product="shop",
        number=42,
        issue_open=True,
        labels=frozenset({"agent-ready"}),
        labeler_trusted=True,
        paused=None,
        live_job=None,
        history=[],
        question=None,
        pr=None,
        now=NOW,
    )
    return TicketSnapshot(**{**base, **kw})


def test_new_ticket_starts_the_engineer():
    assert decide(snap()) == Enqueue("engineer", "implement")


@pytest.mark.parametrize(
    "s, expected",
    [
        (snap(issue_open=False), Wait),
        (snap(issue_open=False, live_job="engineer implement running"), CancelJobs),
        (snap(labels=frozenset()), Wait),
        (snap(labeler_trusted=False), Stop),
        (snap(paused="all agents paused"), Wait),
        (snap(live_job="engineer implement running"), Wait),
        (snap(labels=frozenset({"agent-ready", "split"})), Wait),
    ],
)
def test_gates(s, expected):
    assert isinstance(decide(s), expected)


# Escalation ladder: the tech lead comes before you.


def test_blocked_engineer_goes_to_the_tech_lead_first():
    a = decide(snap(history=[job("engineer", outcome="blocked", questions=["Which currency?"])]))
    assert a == Enqueue("techlead", "clarify", {"questions": ["Which currency?"], "engineer_summary": ""})


def test_tech_lead_answer_goes_back_to_the_engineer():
    h = [
        job("techlead", "clarify", outcome="completed", decision="answer", guidance="Use EUR", minutes_ago=1),
        job("engineer", outcome="blocked", minutes_ago=5),
    ]
    assert decide(snap(history=h)) == Enqueue("engineer", "implement", {"guidance": "Use EUR"})


def test_repeated_blocks_reach_you_only_after_two_clarifications():
    h = [
        job("engineer", outcome="blocked", questions=["still?"], minutes_ago=1),
        job("techlead", "clarify", decision="answer", minutes_ago=2),
        job("engineer", outcome="blocked", minutes_ago=3),
        job("techlead", "clarify", decision="answer", minutes_ago=4),
    ]
    a = decide(snap(history=h))
    assert isinstance(a, Escalate) and "still?" in a.question


@pytest.mark.parametrize("last", [job("engineer", status="failed", outcome="error"), job("engineer", outcome="cap")])
def test_failures_and_caps_go_to_a_split(last):
    assert decide(snap(history=[last])).kind == "techlead"
    assert decide(snap(history=[last])).mode == "split"


def test_failure_after_a_split_reaches_you():
    h = [
        job("engineer", status="failed", outcome="error", minutes_ago=1),
        job("techlead", "split", decision="answer", minutes_ago=2),
    ]
    assert isinstance(decide(snap(history=h)), Escalate)


def test_protected_paths_need_you():
    a = decide(snap(history=[job("engineer", outcome="rejected", reason="touches .github/workflows/ci.yml")]))
    assert isinstance(a, Escalate) and ".github" in a.question


def test_split_ticket_waits_for_its_children():
    assert isinstance(decide(snap(history=[job("techlead", "split", decision="split")])), Wait)


def test_loop_breaker():
    h = [job("engineer", outcome="completed", minutes_ago=i) for i in range(12)]
    assert isinstance(decide(snap(history=h)), Escalate)


# Questions


def test_open_question_parks_the_ticket_and_reversible_defaults_apply_at_the_deadline():
    q = QuestionView(5, "open", None, True, "use EUR", NOW - timedelta(minutes=1), None)
    assert decide(snap(question=q)) == ApplyDefault(5)
    assert isinstance(decide(snap(question=replace(q, deadline=NOW + timedelta(hours=1)))), Wait)
    assert isinstance(decide(snap(question=replace(q, reversible=False))), Wait)


def test_your_answer_is_acted_on_next():
    q = QuestionView(5, "answered", "use EUR", False, None, None, NOW - timedelta(minutes=1))
    old = job("techlead", "clarify", decision="ask_human", minutes_ago=30)
    assert decide(snap(question=q, history=[old])) == Enqueue(
        "engineer", "implement", {"guidance": "The owner answered: use EUR"}
    )
    assert decide(snap(question=q, history=[old], pr=pr())).mode == "rework"


# Pull request lifecycle


def test_ci_grace_then_review():
    assert isinstance(decide(snap(pr=pr(ci="none", head_age_minutes=3))), Wait)  # checks not created yet
    assert decide(snap(pr=pr(ci="success"))) == Enqueue("reviewer", "review", head_sha="h1")
    assert decide(snap(pr=pr(ci="none", head_age_minutes=30))) == Enqueue("reviewer", "review", head_sha="h1")


def test_ci_failure_reruns_once_then_goes_to_the_engineer():
    failing = pr(ci="failure", ci_failed=("tests",), ci_run_ids=(99,))
    assert decide(snap(pr=failing)) == RerunCI("h1")
    a = decide(snap(pr=replace(failing, ci_rerun_done=True)))
    assert a == Enqueue("engineer", "fix-ci", {"ci_failures": ["tests"]}, head_sha="h1")


def test_stuck_ci_reaches_you():
    assert isinstance(decide(snap(pr=pr(ci="pending", head_age_minutes=200))), Escalate)


def test_conflicts_and_stale_branches_fix_themselves():
    assert decide(snap(pr=pr(mergeable_state="dirty"))) == Enqueue("engineer", "update", head_sha="h1")
    assert decide(snap(pr=pr(mergeable_state="behind"))) == UpdateBranch("h1")


def test_rework_rounds_then_arbitration():
    rejected = pr(review="request_changes", review_rounds=1)
    assert decide(snap(pr=rejected)) == Enqueue("engineer", "rework", head_sha="h1")
    assert decide(snap(pr=replace(rejected, review_rounds=3))) == Enqueue("techlead", "arbitrate", head_sha="h1")


def test_arbitration_rulings():
    rejected = pr(review="request_changes", review_rounds=3)
    for_engineer = job("techlead", "arbitrate", decision="side_with_engineer", head="h1")
    assert decide(snap(pr=rejected, history=[for_engineer])) == EvaluatePolicy("h1")
    for_reviewer = job("techlead", "arbitrate", decision="side_with_reviewer", guidance="add the test", head="h1")
    assert decide(snap(pr=rejected, history=[for_reviewer])) == Enqueue(
        "engineer", "rework", {"guidance": "add the test"}, head_sha="h1"
    )
    second = [
        job("techlead", "arbitrate", decision="side_with_reviewer", head="h0"),
        job("techlead", "arbitrate", decision="side_with_reviewer", head="h-1"),
    ]
    assert isinstance(decide(snap(pr=rejected, history=second)), Escalate)


def test_rework_that_goes_nowhere_climbs_the_ladder_instead_of_looping():
    stuck = job("engineer", "rework", outcome="no_changes", head="h1", summary="reviewer is wrong")
    a = decide(snap(pr=pr(review="request_changes", review_rounds=1), history=[stuck]))
    assert a.kind == "techlead" and a.mode == "clarify" and a.head_sha == "h1"


def test_approval_runs_the_policy_then_waits():
    approved = pr(review="approve")
    assert decide(snap(pr=approved)) == EvaluatePolicy("h1")
    assert isinstance(decide(snap(pr=replace(approved, policy=PolicyView("auto", True)))), Wait)
    waiting = replace(approved, policy=PolicyView("human", False))
    assert decide(snap(pr=waiting)) == Wait("waiting for your approval")
    assert decide(snap(pr=replace(waiting, human_approved=True))) == EvaluatePolicy("h1")


def test_closed_and_merged_prs():
    assert isinstance(decide(snap(pr=pr(closed=True))), Stop)
    assert isinstance(decide(snap(pr=pr(closed=True, merged=True))), Wait)


def test_reviewer_failure_reaches_you():
    failed = job("reviewer", "review", status="failed", outcome="error", head="h1", error="boom")
    assert isinstance(decide(snap(pr=pr(), history=[failed])), Escalate)


def test_epic_breakdown():
    epic = frozenset({"needs-breakdown"})
    assert decide(snap(labels=epic)) == Enqueue("techlead", "breakdown")
    failed = job("techlead", "breakdown", status="failed", outcome="error")
    assert isinstance(decide(snap(labels=epic, history=[failed])), Escalate)
