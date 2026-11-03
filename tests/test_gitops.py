"""The trusted side of the sandbox boundary: bundles in, bundles out, verify, push.

`test_review_attack_is_rejected` replays the attack from the Nov 2 review (move origin/main, edit
CI, plant a hook) against the new design: the agent's tricks live only in its own copy and cannot
influence the worker's checks or run in the worker's repository.
"""

import subprocess
from pathlib import Path

import pytest

from core.gitops import GitWorkspace, RejectedResult


def sh(cwd: Path, *cmd: str) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A bare "GitHub" repo with one commit on main, reachable through github_git_url."""
    from core.settings import get_settings

    origin = tmp_path / "github" / "acme" / "shop.git"
    seed = tmp_path / "seed"
    origin.mkdir(parents=True)
    sh(origin, "git", "init", "-q", "--bare", "-b", "main")
    sh(tmp_path, "git", "clone", "-q", str(origin), str(seed))
    sh(seed, "git", "config", "user.email", "a@b")
    sh(seed, "git", "config", "user.name", "a")
    (seed / "app.py").write_text("print(1)\n")
    (seed / "CLAUDE.md").write_text("Run `pytest`.\n")
    (seed / "docs" / "brain").mkdir(parents=True)
    (seed / "docs" / "brain" / "product-brief.md").write_text("A shop.\n")
    sh(seed, "git", "add", ".")
    sh(seed, "git", "commit", "-qm", "init")
    sh(seed, "git", "push", "-q", "origin", "main")
    monkeypatch.setenv("GITHUB_GIT_URL", str(tmp_path / "github" / "{repo}.git"))
    get_settings.cache_clear()
    yield origin
    get_settings.cache_clear()


def worker_side(tmp_path: Path) -> tuple[GitWorkspace, str]:
    ws = GitWorkspace(tmp_path / "worker" / "repo", "acme/shop")
    ws.init()
    return ws, ws.fetch("refs/heads/main", "refs/remotes/base")


def sandbox_side(tmp_path: Path, bundle: Path) -> Path:
    """What the runner does: clone the bundle and work on `work`."""
    work = tmp_path / "sandbox"
    sh(tmp_path, "git", "clone", "-q", str(bundle), str(work))
    sh(work, "git", "checkout", "-q", "-B", "work", "origin/work")
    sh(work, "git", "config", "user.email", "agent@x")
    sh(work, "git", "config", "user.name", "agent")
    return work


def export(work: Path, start: str, out: Path) -> Path:
    sh(work, "git", "update-ref", "refs/heads/agent-result", "HEAD")
    sh(work, "git", "bundle", "create", "-q", str(out), "refs/heads/agent-result", f"^{start}")
    return out


def commit(work: Path, path: str, text: str) -> None:
    (work / path).parent.mkdir(parents=True, exist_ok=True)
    (work / path).write_text(text)
    sh(work, "git", "add", "-A")
    sh(work, "git", "commit", "-qm", f"edit {path}")


def test_round_trip_push(tmp_path, remote):
    ws, base = worker_side(tmp_path)
    ws.input_bundle(tmp_path / "in.bundle", base, base)
    work = sandbox_side(tmp_path, tmp_path / "in.bundle")
    commit(work, "app.py", "print(2)\n")
    head = ws.import_result(export(work, base, tmp_path / "out.bundle"), start_sha=base)
    assert ws.check_pushable(base, head) == ["app.py"]
    ws.push(head, "agent/issue-1")
    assert sh(remote, "git", "rev-parse", "agent/issue-1") == head


def test_project_instructions_come_from_the_base(tmp_path, remote):
    ws, base = worker_side(tmp_path)
    text = ws.project_instructions(base)
    assert "Run `pytest`." in text and "## product-brief.md" in text and "A shop." in text


def test_review_attack_is_rejected(tmp_path, remote):
    ws, base = worker_side(tmp_path)
    ws.input_bundle(tmp_path / "in.bundle", base, base)
    work = sandbox_side(tmp_path, tmp_path / "in.bundle")
    commit(work, ".github/workflows/ci.yml", "evil")
    # Everything that fooled the old runner, done inside the agent's own copy:
    sh(work, "git", "fetch", "-q", ".", "HEAD:refs/remotes/origin/base")
    hook = work / ".hooks" / "pre-push"
    hook.parent.mkdir()
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path / 'hook-ran'}\n")
    hook.chmod(0o755)
    sh(work, "git", "config", "core.hooksPath", ".hooks")

    head = ws.import_result(export(work, base, tmp_path / "out.bundle"), start_sha=base)
    with pytest.raises(RejectedResult, match=r"\.github/workflows/ci\.yml"):
        ws.check_pushable(base, head)  # the worker diffs against the base it fetched itself
    assert not (tmp_path / "hook-ran").exists()
    assert "agent/issue-1" not in sh(remote, "git", "branch")


def test_history_rewrites_are_rejected(tmp_path, remote):
    ws, base = worker_side(tmp_path)
    ws.input_bundle(tmp_path / "in.bundle", base, base)
    work = sandbox_side(tmp_path, tmp_path / "in.bundle")
    commit(work, "app.py", "first\n")
    first = sh(work, "git", "rev-parse", "HEAD")
    ws.push(ws.import_result(export(work, base, tmp_path / "a.bundle"), base), "agent/issue-1")

    # Next round starts from the pushed branch; the agent rewrites it instead of adding commits.
    sh(work, "git", "reset", "-q", "--hard", base)
    commit(work, "app.py", "rewritten\n")
    with pytest.raises(RejectedResult, match="rewrite history"):
        ws.import_result(export(work, base, tmp_path / "b.bundle"), start_sha=first)


def test_garbage_and_oversized_bundles_are_rejected(tmp_path, remote, monkeypatch):
    from core.settings import get_settings

    ws, base = worker_side(tmp_path)
    junk = tmp_path / "junk.bundle"
    junk.write_bytes(b"not a bundle")
    with pytest.raises(RejectedResult, match="unusable"):
        ws.import_result(junk, base)
    monkeypatch.setenv("MAX_BUNDLE_MB", "0")
    get_settings.cache_clear()
    with pytest.raises(RejectedResult, match="larger than"):
        ws.import_result(junk, base)


def test_push_never_forces(tmp_path, remote):
    ws, base = worker_side(tmp_path)
    ws.input_bundle(tmp_path / "in.bundle", base, base)
    work = sandbox_side(tmp_path, tmp_path / "in.bundle")
    commit(work, "a.txt", "a")
    a = ws.import_result(export(work, base, tmp_path / "a.bundle"), base)
    ws.push(a, "agent/issue-1")
    sh(work, "git", "reset", "-q", "--hard", base)
    commit(work, "b.txt", "b")
    b = ws.import_result(export(work, base, tmp_path / "b.bundle"), base)
    with pytest.raises(Exception, match="push failed"):
        ws.push(b, "agent/issue-1")  # not a fast-forward
