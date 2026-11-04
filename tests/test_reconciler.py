"""The reconciler end to end against real Postgres and an in-memory GitHub, with the worker's part
played by hand. These replace the old Temporal workflow tests."""

from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

from core.github import CIState
from tests.conftest import needs_db
from tests.fakes import FakeChat, FakeGitHub

pytestmark = [pytest.mark.db, needs_db]


@pytest.fixture
async def env(db):
    from core.settings import get_settings
    from orchestrator import state
    from orchestrator.context import Ctx
    from orchestrator.reconciler import Reconciler

    gh, chat = FakeGitHub(), FakeChat()
    rec = Reconciler(Ctx(gh=gh, chat=chat, settings=get_settings(), gh_for=lambda role: gh))
    return rec, gh, chat, await state.product("shop")


async def sweep(rec, product):
    await rec.sweep_product(product)


async def run_job(kind: str, result: dict):
    """Play the worker: claim the queued job and finish it with `result`."""
    from orchestrator import jobs

    job = await jobs.claim("w")
    assert job is not None and job.kind == kind, job
    await jobs.succeed(job.id, "w", result)
    return job


async def live():
    from orchestrator import state

    return await state.q("SELECT kind, mode, ticket, pr, head_sha, input FROM jobs WHERE status = 'queued'")


async def to_approved_pr(rec, gh, product, files=(("docs/guide.md", 5),)):
    """Issue -> engineer -> PR -> CI green -> reviewer approves. Returns (issue, pr)."""
    n = gh.add_issue()
    await sweep(rec, product)
    assert [(j["kind"], j["mode"]) for j in await live()] == [("engineer", "implement")]
    pr = gh.add_pr(f"agent/issue-{n}", "h1", files=files)
    await run_job("engineer", {"outcome": "completed", "pushed": True, "head_sha": "h1", "pr": pr})

    await sweep(rec, product)
    assert await live() == []  # CI not reported yet: within the grace period, so wait
    gh.ci["h1"] = CIState("success")
    await sweep(rec, product)
    [reviewer] = await live()
    assert (reviewer["kind"], reviewer["pr"], reviewer["head_sha"]) == ("reviewer", pr, "h1")
    await run_job(
        "reviewer", {"outcome": "completed", "verdict": "approve", "risk": "low", "pr": pr, "summary": "Looks right."}
    )
    return n, pr


async def test_level_0_needs_you_once_then_merges_and_records_the_shadow_verdict(env):
    from orchestrator import state

    rec, gh, chat, product = env
    n, pr = await to_approved_pr(rec, gh, product)

    await sweep(rec, product)
    assert gh.checks[-1][1] is None and "Waiting for your approval" in gh.checks[-1][2]
    assert len(await state.pending_approvals()) == 1 and any("Approval needed" in p for p in chat.posts)
    await sweep(rec, product)  # nothing new: no second request, no second comment
    assert len([c for c in gh.prs[pr]["comments"] if "approval is needed" in c["body"]]) == 1

    gh.reviews[pr] = [{"user": {"login": "owner"}, "state": "APPROVED", "commit_id": "h1"}]
    await sweep(rec, product)
    assert gh.checks[-1][1] == "success" and f"PR_{pr}" in gh.auto_merge
    assert await state.pending_approvals() == []

    gh.merge(pr)
    await sweep(rec, product)
    merge = await state.merge_record("shop", pr)
    assert merge["auto"] is False and merge["next_level_tier"] == "auto"  # would have auto-merged at level 1
    assert "agent-ready" not in gh.labels_of(n)
    gh.ci[merge["merge_sha"]] = CIState("success")
    await sweep(rec, product)
    assert (await state.merge_record("shop", pr))["outcome"] == "ok"


async def test_level_1_auto_merges_and_a_bad_merge_is_reverted_and_demoted(env):
    from orchestrator import state

    rec, gh, chat, product = env
    await state.set_autonomy("shop", 1, False, "human:test", "test")
    n, pr = await to_approved_pr(rec, gh, product)
    await sweep(rec, product)
    assert gh.checks[-1][1] == "success" and f"PR_{pr}" in gh.auto_merge
    assert await state.pending_approvals() == []  # you were never asked

    gh.merge(pr)
    await sweep(rec, product)
    gh.issues[n]["state"] = "closed"
    gh.ci[f"merge-{pr}"] = CIState("failure", ("unit-tests",))
    await sweep(rec, product)

    merge = await state.merge_record("shop", pr)
    assert merge["outcome"] == "broke_main" and merge["revert_pr"]
    revert = gh.prs[merge["revert_pr"]]
    assert "agent-revert" in {lbl["name"] for lbl in revert["labels"]}
    autonomy = await state.autonomy("shop")
    assert autonomy["level"] == 0 and autonomy["auto_merge_paused"] and autonomy["changed_by"] == "auto-demotion"
    assert gh.issues[n]["state"] == "open" and "agent-ready" in gh.labels_of(n)
    assert any("Reverting" in p for p in chat.posts)

    gh.ci[revert["head"]["sha"]] = CIState("success")
    await sweep(rec, product)
    assert revert["node_id"] in gh.auto_merge  # the revert merges itself once its CI passes


async def test_risky_change_always_needs_you_even_at_level_2(env):
    from orchestrator import state

    rec, gh, chat, product = env
    await state.set_autonomy("shop", 2, False, "human:test", "test")
    _, pr = await to_approved_pr(rec, gh, product, files=(("src/auth/login.py", 12),))
    await sweep(rec, product)
    assert gh.checks[-1][1] is None
    [approval] = await state.pending_approvals()
    assert any("sensitive" in r for r in approval["payload"]["reasons"])


async def test_config_drift_fails_closed_and_alerts_once(env):
    from orchestrator import state

    rec, gh, chat, product = env
    await state.set_autonomy("shop", 2, False, "human:test", "test")
    gh.rules = [r for r in gh.rules if r["type"] != "required_status_checks"]
    await to_approved_pr(rec, gh, product)
    await sweep(rec, product)
    await sweep(rec, product)
    assert gh.checks[-1][1] is None and not gh.auto_merge
    assert sum("protection settings are incomplete" in p for p in chat.posts) == 1


async def test_untrusted_labeler_is_stopped(env):
    rec, gh, chat, product = env
    n = gh.add_issue(actor="drive-by-user")
    await sweep(rec, product)
    assert await live() == [] and "agent-ready" not in gh.labels_of(n)
    assert "trusted list" in gh.issues[n]["comments"][-1]["body"]


async def test_a_fork_pr_cannot_touch_a_ticket(env):
    rec, gh, chat, product = env
    n = gh.add_issue()
    await sweep(rec, product)
    fork = gh.add_pr(f"agent/issue-{n}", "evil", repo="stranger/shop")
    gh.prs[fork]["state"] = "closed"
    await sweep(rec, product)
    assert [(j["kind"], j["ticket"]) for j in await live()] == [("engineer", n)]
    assert "agent-ready" in gh.labels_of(n) and gh.issues[n]["comments"] == []


async def test_blocked_engineer_tech_lead_asks_you_and_your_answer_unblocks(env):
    from orchestrator import actions, state

    rec, gh, chat, product = env
    n = gh.add_issue()
    await sweep(rec, product)
    await run_job("engineer", {"outcome": "blocked", "questions": ["EUR or USD?"]})
    await sweep(rec, product)
    [tl] = await live()
    assert (tl["kind"], tl["mode"], tl["input"]["questions"]) == ("techlead", "clarify", ["EUR or USD?"])

    # The tech lead can't answer from the brain, so it asks you (as the worker does).
    await actions.ask_human(rec.ctx, product, n, "Which currency?", ["EUR", "USD"], "EUR", reversible=True)
    await run_job("techlead", {"outcome": "completed", "decision": "ask_human"})
    await sweep(rec, product)
    assert await live() == [] and "needs-human" in gh.labels_of(n)

    gh.issues[n]["comments"].append({"user": {"login": "owner"}, "body": "/answer 2"})
    await sweep(rec, product)
    [eng] = await live()
    assert eng["kind"] == "engineer" and eng["input"]["guidance"] == "The owner answered: USD"
    assert "needs-human" not in gh.labels_of(n)
    assert any("USD" in m for m in await state.recent_memories("shop"))


async def test_kill_switch_stops_new_work(env):
    from orchestrator import state

    rec, gh, chat, product = env
    await state.pause(None, "testing", "human:test")
    gh.add_issue()
    await sweep(rec, product)
    assert await live() == []
    await state.resume(None, "human:test")
    await sweep(rec, product)
    assert len(await live()) == 1


async def test_promotion_is_proposed_from_the_track_record_and_needs_you(env):
    from orchestrator import admin, state, sweeps

    rec, gh, chat, product = env
    await state.q("UPDATE autonomy SET changed_at = now() - interval '20 days' WHERE product = 'shop'")
    await state.autonomy("shop")
    await state.q("UPDATE autonomy SET changed_at = now() - interval '20 days' WHERE product = 'shop'")
    for i in range(20):
        await state.record_merge(
            "shop", 100 + i, i, f"m{i}", False, 0, "auto", datetime.now(UTC) - timedelta(days=5), timedelta(hours=2)
        )
        await state.set_merge_outcome("shop", 100 + i, "ok")
    await sweeps.propose_promotions(rec.ctx, product)
    [approval] = await state.pending_approvals()
    assert approval["kind"] == "promotion" and approval["payload"]["to"] == 1
    assert (await state.autonomy("shop"))["level"] == 0  # nothing changes until you approve

    out = await admin.run(Namespace(cmd="approve", id=approval["id"]))
    assert out == "shop promoted to level 1" and (await state.autonomy("shop"))["level"] == 1


async def test_ci_failure_rerun_then_fix(env):
    rec, gh, chat, product = env
    n = gh.add_issue()
    await sweep(rec, product)
    pr = gh.add_pr(f"agent/issue-{n}", "h1")
    await run_job("engineer", {"outcome": "completed", "pushed": True, "head_sha": "h1", "pr": pr})
    gh.ci["h1"] = CIState("failure", ("unit",), (55,))
    await sweep(rec, product)
    assert gh.reruns == [55] and await live() == []
    await sweep(rec, product)
    [fix] = await live()
    assert (fix["kind"], fix["mode"], fix["input"]["ci_failures"]) == ("engineer", "fix-ci", ["unit"])


async def test_digest_lists_what_needs_you(env):
    from orchestrator import state
    from orchestrator.digest import build_digest

    rec, gh, chat, product = env
    await state.create_question("shop", 3, "Which currency?", [], None, False, None)
    text = await build_digest()
    assert "Needs you* (1)" in text and "Which currency?" in text and "level 0" in text


async def test_a_clean_branch_update_keeps_the_review_but_a_new_push_does_not(env):
    from orchestrator import state

    rec, gh, chat, product = env
    await state.set_autonomy("shop", 1, False, "human:test", "test")
    _, pr = await to_approved_pr(rec, gh, product)
    gh.prs[pr]["mergeable_state"] = "behind"  # main moved on before the merge
    await sweep(rec, product)
    assert gh.prs[pr]["head"]["sha"] == "h1+base"
    await sweep(rec, product)
    assert await live() == []  # no second paid review for our own clean update
    assert gh.checks[-1][0] == "h1+base" and gh.checks[-1][1] == "success"  # the approval carried over

    gh.prs[pr]["head"]["sha"] = "h9"  # anything else on the branch gets a fresh review
    gh.ci["h9"] = CIState("success")
    await sweep(rec, product)
    [again] = await live()
    assert (again["kind"], again["head_sha"]) == ("reviewer", "h9")
