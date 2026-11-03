import pytest

from orchestrator.policy import AUTO, HUMAN, ChangedFile, PolicyError, PRFacts, evaluate, load_policy

POLICY = load_policy(None)


def facts(*files: tuple[str, int], ci="success", external=False, risk="low", agent=True, revert=False) -> PRFacts:
    return PRFacts(tuple(ChangedFile(p, n) for p, n in files), ci, external, risk, agent, revert)


def test_level_0_is_shadow_mode():
    d = evaluate(POLICY, facts(("docs/guide.md", 5)), level=0, auto_merge_paused=False, config_ok=True)
    assert d.tier == HUMAN
    assert d.next_level_tier == AUTO  # recorded as "would have auto-merged": the track record for promotion


def test_level_1_auto_merges_only_docs_and_tests():
    assert evaluate(POLICY, facts(("README.md", 10), ("tests/test_x.py", 20)), 1, False, True).tier == AUTO
    d = evaluate(POLICY, facts(("src/app.py", 3)), 1, False, True)
    assert d.tier == HUMAN and "only auto-merges docs and tests" in d.reasons[0]
    assert d.next_level_tier == AUTO  # level 2 would take it


def test_level_2_size_limits():
    assert evaluate(POLICY, facts(("src/app.py", 120), ("tests/test_app.py", 80)), 2, False, True).tier == AUTO
    big = evaluate(POLICY, facts(("src/app.py", 400)), 2, False, True)
    assert big.tier == HUMAN and "over the level-2 limit" in big.reasons[0]
    many = evaluate(POLICY, facts(*[(f"src/m{i}.py", 1) for i in range(16)]), 2, False, True)
    assert many.tier == HUMAN


@pytest.mark.parametrize(
    "f, reason",
    [
        (facts(("src/auth/session.py", 2)), "sensitive"),
        (facts(("app/billing/invoice.py", 2)), "sensitive"),
        (facts(("db/migrations/0004.py", 2)), "sensitive"),
        (facts(("package.json", 1)), "dependencies"),
        (facts(("services/api/requirements.txt", 1)), "dependencies"),
        (facts(("src/app.py", 1), ci="none"), "no passing CI"),
        (facts(("src/app.py", 1), ci="failure"), "CI is failure"),
        (facts(("src/app.py", 1), external=True), "outside input"),
        (facts(("src/app.py", 1), risk="medium"), "risk medium"),
        (facts(("src/app.py", 1), agent=False), "not written by the engineer agent"),
        (facts((".agent-company/policy.yaml", 1)), "sensitive"),
        (facts(("docs/brain/roadmap.md", 1)), "sensitive"),
    ],
)
def test_floors_force_a_human_at_every_level(f, reason):
    for level in range(4):
        d = evaluate(POLICY, f, level, False, True)
        assert d.tier == HUMAN and d.next_level_tier == HUMAN
        assert any(reason in r for r in d.reasons), d.reasons


def test_paused_or_unverified_config_falls_back_to_human():
    f = facts(("README.md", 1))
    paused = evaluate(POLICY, f, 2, auto_merge_paused=True, config_ok=True)
    assert paused.tier == HUMAN and "paused" in paused.reasons[0]
    drift = evaluate(POLICY, f, 2, auto_merge_paused=False, config_ok=False)
    assert drift.tier == HUMAN and "could not be verified" in drift.reasons[0]


def test_reverts_of_auto_merges_go_through_but_not_past_floors():
    assert evaluate(POLICY, facts(("src/app.py", 900), revert=True), 0, False, True).tier == AUTO
    assert evaluate(POLICY, facts(("src/app.py", 9), ci="pending", revert=True), 0, False, True).tier == HUMAN
    assert evaluate(POLICY, facts(("db/migrations/1.py", 9), revert=True), 0, False, True).tier == HUMAN


def test_product_policy_overrides_and_validation():
    p = load_policy("sensitive_paths: ['src/payments/**']\nauto_merge:\n  level_2: {max_changed_lines: 10}\n")
    assert p["sensitive_paths"] == ["src/payments/**"]
    assert p["auto_merge"]["level_2"] == {"max_changed_lines": 10}
    assert p["auto_merge"]["level_1"]["max_changed_lines"] == 50  # untouched defaults stay
    assert evaluate(p, facts(("src/payments/x.py", 1)), 2, False, True).tier == HUMAN
    for bad in ("- a list", "sensitive_paths: 3", "surprise: []", "auto_merge: {level_9: {}}", "x: [unclosed"):
        with pytest.raises(PolicyError):
            load_policy(bad)
