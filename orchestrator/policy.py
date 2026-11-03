"""Merge policy: which PRs may merge without a human. Pure functions, no I/O.

Deterministic facts set the floor (sensitive paths, dependency or migration changes, missing CI,
outsider-originated work); the reviewer's risk opinion can only raise the tier. The policy file
is read from the *base* branch (`.agent-company/policy.yaml`), so a PR cannot loosen its own rules.
"""

from dataclasses import dataclass, field

import yaml

from agents._shared.guards import path_matches

POLICY_PATH = ".agent-company/policy.yaml"
AUTO, HUMAN = "auto", "human"
MAX_LEVEL = 3

DEFAULT_POLICY: dict = {
    "sensitive_paths": [
        ".agent-company/**",
        ".github/**",
        "infra/**",
        "docs/brain/**",
        "CLAUDE.md",
        "CODEOWNERS",
        "**/auth/**",
        "**/billing/**",
        "**/payments/**",
        "**/migrations/**",
        "**/*.sql",
    ],
    "auto_merge": {
        "level_1": {
            "max_changed_lines": 50,
            "max_files": 10,
            "only_paths": ["**/*.md", "docs/**", "tests/**", "**/test_*"],
        },
        "level_2": {"max_changed_lines": 300, "max_files": 15},
    },
    "dependency_files": [
        "**/package.json",
        "**/package-lock.json",
        "**/pnpm-lock.yaml",
        "**/yarn.lock",
        "**/requirements*.txt",
        "**/pyproject.toml",
        "**/uv.lock",
        "**/poetry.lock",
        "**/Pipfile*",
        "**/go.mod",
        "**/go.sum",
        "**/Cargo.toml",
        "**/Cargo.lock",
        "**/Gemfile*",
        "**/composer.json",
        "**/composer.lock",
    ],
    "migration_paths": ["**/migrations/**", "**/alembic/**", "**/*.sql"],
}


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ChangedFile:
    path: str
    changed_lines: int


@dataclass(frozen=True)
class PRFacts:
    files: tuple[ChangedFile, ...]
    ci: str  # success, failure, pending, none
    external_origin: bool  # the ticket started from outsider input
    reviewer_risk: str  # low, medium, high
    authored_by_agent: bool
    is_revert_of_auto_merge: bool = False


@dataclass(frozen=True)
class Decision:
    tier: str  # what applies now
    next_level_tier: str  # what would apply one level up (the shadow record that earns promotion)
    reasons: tuple[str, ...] = field(default_factory=tuple)


def load_policy(text: str | None) -> dict:
    """The product's policy merged over the defaults. Unknown or malformed content raises."""
    policy = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULT_POLICY.items()}
    if not text:
        return policy
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise PolicyError(f"{POLICY_PATH} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise PolicyError(f"{POLICY_PATH} must be a mapping")
    unknown = set(data) - set(DEFAULT_POLICY)
    if unknown:
        raise PolicyError(f"{POLICY_PATH} has unknown keys: {', '.join(sorted(unknown))}")
    for key, value in data.items():
        if key == "auto_merge":
            if not isinstance(value, dict) or set(value) - {"level_1", "level_2"}:
                raise PolicyError("auto_merge may only define level_1 and level_2")
            policy["auto_merge"].update(value)
        elif not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise PolicyError(f"{key} must be a list of glob strings")
        else:
            policy[key] = value
    return policy


def _matches_any(path: str, globs: list[str]) -> bool:
    return any(path_matches(path, g) for g in globs)


def floors(policy: dict, facts: PRFacts) -> list[str]:
    """Reasons that require a human at every level."""
    reasons = []
    sensitive = [f.path for f in facts.files if _matches_any(f.path, policy["sensitive_paths"])]
    if sensitive:
        reasons.append("touches sensitive paths: " + ", ".join(sensitive[:10]))
    if any(_matches_any(f.path, policy["dependency_files"]) for f in facts.files):
        reasons.append("changes dependencies")
    if any(_matches_any(f.path, policy["migration_paths"]) for f in facts.files):
        reasons.append("includes a database migration")
    if facts.ci != "success":
        reasons.append("no passing CI" if facts.ci == "none" else f"CI is {facts.ci}")
    if facts.reviewer_risk in ("medium", "high"):
        reasons.append(f"reviewer rated the risk {facts.reviewer_risk}")
    if not facts.is_revert_of_auto_merge:
        if facts.external_origin:
            reasons.append("the work started from outside input")
        if not facts.authored_by_agent:
            reasons.append("not written by the engineer agent")
    return reasons


def _tier_at(policy: dict, facts: PRFacts, level: int, base: list[str]) -> tuple[str, list[str]]:
    if base:
        return HUMAN, base
    if facts.is_revert_of_auto_merge:
        return AUTO, ["reverts an auto-merged change to restore a known-good state"]
    if level <= 0:
        return HUMAN, ["autonomy level 0 (shadow): every merge needs you"]
    rules = policy["auto_merge"]["level_1" if level == 1 else "level_2"]
    lines = sum(f.changed_lines for f in facts.files)
    reasons = []
    if lines > rules.get("max_changed_lines", 0):
        reasons.append(f"{lines} changed lines is over the level-{min(level, 2)} limit of {rules['max_changed_lines']}")
    if len(facts.files) > rules.get("max_files", 10**6):
        reasons.append(f"{len(facts.files)} files is over the limit of {rules['max_files']}")
    only = rules.get("only_paths")
    if only:
        outside = [f.path for f in facts.files if not _matches_any(f.path, only)]
        if outside:
            reasons.append(f"level {level} only auto-merges docs and tests; also changes " + ", ".join(outside[:5]))
    if reasons:
        return HUMAN, reasons
    return AUTO, [f"low risk at autonomy level {level}"]


def evaluate(policy: dict, facts: PRFacts, level: int, auto_merge_paused: bool, config_ok: bool) -> Decision:
    base = floors(policy, facts)
    next_tier, _ = _tier_at(policy, facts, min(level + 1, MAX_LEVEL), base)
    tier, reasons = _tier_at(policy, facts, level, base)
    if tier == AUTO and auto_merge_paused:
        tier, reasons = HUMAN, ["auto-merge is paused for this product after a bad merge", *reasons]
    if tier == AUTO and not config_ok:
        tier, reasons = HUMAN, ["the repository's GitHub protection settings could not be verified", *reasons]
    return Decision(tier, next_tier, tuple(reasons))


def risk_floor_only_rises(opinion: str | None) -> str:
    """Normalize a model's risk opinion; anything unexpected counts as high."""
    return opinion if opinion in ("low", "medium", "high") else "high"
