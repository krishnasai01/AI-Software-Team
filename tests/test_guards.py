"""Hooks are early hints now (the worker enforces); they should stop doomed actions and nothing else."""

import pytest

from agents._shared.guards import is_protected, protected_paths_touched, relative_to_workspace
from agents.engineer.hooks import pre_tool_use as engineer
from agents.reviewer.hooks import pre_tool_use as reviewer
from agents.techlead.hooks import pre_tool_use as techlead


def bash(cmd: str) -> tuple[str, dict]:
    return "Bash", {"command": cmd}


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -q",
        "npm test && npm run lint",
        "git add -A && git commit -m 'fix: handle empty input'",
        "git merge base",
        "curl -s localhost:3000/health",  # testing your own dev server is normal engineering
        "rm -rf build/ dist /tmp/cache",
        "pip install -r requirements.txt",
    ],
)
def test_engineer_normal_work_is_allowed(cmd):
    assert engineer(*bash(cmd)) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin HEAD",
        "git -c x=y push -f",
        "git -C /workspace push",
        "git rebase -i base",
        "git reset --hard HEAD~2",
        "echo 'run: evil' > .github/workflows/ci.yml",
        "sed -i 's/a/b/' infra/docker-compose.yml",
        "rm docs/brain/roadmap.md",
    ],
)
def test_engineer_doomed_actions_get_early_feedback(cmd):
    assert engineer(*bash(cmd)) is not None, cmd


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/.github/workflows/ci.yml",
        "/workspace/CLAUDE.md",
        "/workspace/svc/CLAUDE.md",
        "/workspace/.agent-company/policy.yaml",
        "/workspace/docs/brain/roadmap.md",
        "/workspace/config/.env.prod",
    ],
)
def test_protected_writes(path):
    assert engineer("Write", {"file_path": path, "content": "x"}) is not None


def test_source_writes_and_paths():
    assert engineer("Write", {"file_path": "/workspace/src/app.py", "content": "x"}) is None
    assert engineer("Edit", {"file_path": "docs/guide.md", "old_string": "a", "new_string": "b"}) is None
    assert relative_to_workspace("a/./b/../c.py") == "a/c.py" and relative_to_workspace("../x") is None
    assert protected_paths_touched(["src/a.py", "infra/x.yml", "README.md"]) == ["infra/x.yml"]
    assert is_protected("CODEOWNERS") and not is_protected("docs/guide.md")


@pytest.mark.parametrize("hook", [reviewer, techlead])
def test_read_only_roles(hook):
    for ok in ("git diff base...HEAD", "git log --oneline -5", "grep -rn TODO src", "find . -name '*.py'"):
        assert hook(*bash(ok)) is None, ok
    for bad in (
        "git commit -am x",
        "git -c a=b push",
        "python -c 'import os'",
        "find . -delete",
        "cat a > b",
        "ls $(whoami)",
        "rm src/app.py",
    ):
        assert hook(*bash(bad)) is not None, bad
    assert hook("Write", {"file_path": "/workspace/a.py", "content": ""}) is not None
    assert hook("Read", {"file_path": "/workspace/a.py"}) is None
