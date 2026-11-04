"""The real worker and real Docker sandbox, with the rehearsal image standing in for a model and a
local bare repository standing in for GitHub's git server. This is the $0 rehearsal, run locally.

Needs the images:
  docker build -f sandbox-images/base/Dockerfile -t agent-company/sandbox-base:test .
  docker build --build-arg BASE=agent-company/sandbox-base:test -t agent-company/sandbox-rehearsal:test \
      sandbox-images/rehearsal
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from core.github import CIState
from tests.conftest import needs_db
from tests.fakes import FakeChat, FakeGitHub

IMAGE = "agent-company/sandbox-rehearsal:test"


def _image_present() -> bool:
    return (
        bool(shutil.which("docker"))
        and subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
    )


pytestmark = [pytest.mark.db, needs_db, pytest.mark.skipif(not _image_present(), reason=f"{IMAGE} not built")]


def sh(cwd: Path, *cmd: str) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeProxy:
    def __init__(self):
        self.issued, self.revoked = [], []

    async def issue_key(self, run_id, team_id, max_budget_usd, hours=2):
        self.issued.append((run_id, max_budget_usd))
        return f"sk-run-{run_id}"

    async def key_spend(self, key):
        return 0.0

    async def revoke(self, key):
        self.revoked.append(key)


@pytest.fixture
async def worker(db, tmp_path, monkeypatch):
    from core.settings import get_settings
    from orchestrator.context import Ctx
    from orchestrator.worker import Worker

    origin = tmp_path / "github" / "acme" / "shop.git"
    origin.mkdir(parents=True)
    sh(origin, "git", "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(seed))
    sh(seed, "git", "config", "user.email", "a@b")
    sh(seed, "git", "config", "user.name", "a")
    (seed / "CLAUDE.md").write_text("Run pytest.\n")
    sh(seed, "git", "add", ".")
    sh(seed, "git", "commit", "-qm", "init")
    sh(seed, "git", "push", "-q", "origin", "main")
    origin.chmod(0o777)

    monkeypatch.setenv("GITHUB_GIT_URL", str(tmp_path / "github" / "{repo}.git"))
    monkeypatch.setenv("SANDBOX_RUNTIME", "runc")
    monkeypatch.setenv("SANDBOX_NETWORK", "none")  # the scripted agent needs no network at all
    get_settings.cache_clear()
    gh, proxy = FakeGitHub(), FakeProxy()
    w = Worker(
        Ctx(gh=gh, chat=FakeChat(), settings=get_settings(), gh_for=lambda role: gh),
        proxy=proxy,
        git_token=lambda kind, repo: None,
    )
    return w, gh, proxy, origin


async def claim_and_run(w, kind, mode, ticket, **kw):
    from orchestrator import jobs

    await jobs.enqueue(kind, mode, "shop", ticket, **kw)
    job = await jobs.claim(w.worker_id)
    await w.run_job(job)
    return await jobs.get(job.id)


async def test_engineer_reviewer_and_tech_lead_through_the_real_sandbox(worker):
    from orchestrator import state

    w, gh, proxy, origin = worker
    n = gh.add_issue("Add a note")

    eng = await claim_and_run(w, "engineer", "implement", n)
    assert eng.status == "succeeded", eng.error
    assert eng.result["outcome"] == "completed" and eng.result["changed_files"] == ["docs/rehearsal.md"]
    head = eng.result["head_sha"]
    assert sh(origin, "git", "rev-parse", f"agent/issue-{n}") == head  # pushed by the worker, not the sandbox
    pr = gh.prs[eng.result["pr"]]
    assert pr["head"]["ref"] == f"agent/issue-{n}" and f"Closes #{n}" in pr["body"]
    assert proxy.revoked == [f"sk-run-{eng.result['run_id']}"]  # the run's model key is always revoked
    run = await state.q1("SELECT * FROM runs WHERE id = %s", (eng.result["run_id"],))
    assert run["outcome"] == "success" and run["job_id"] == eng.id

    # Second round builds on the pushed branch (fast-forward only).
    again = await claim_and_run(w, "engineer", "rework", n, pr=pr["number"], head_sha=head)
    assert again.result["outcome"] == "completed"
    assert sh(origin, "git", "merge-base", "--is-ancestor", head, f"agent/issue-{n}") == ""

    # The reviewer sees the PR head; GitHub exposes it as refs/pull/N/head.
    new_head = again.result["head_sha"]
    sh(origin, "git", "update-ref", f"refs/pull/{pr['number']}/head", new_head)
    gh.ci[new_head] = CIState("success")
    rev = await claim_and_run(w, "reviewer", "review", n, pr=pr["number"], head_sha=new_head)
    assert rev.result["verdict"] == "approve" and rev.result["risk"] == "low"
    assert gh.posted_reviews == [(pr["number"], new_head, "APPROVE")]

    tl = await claim_and_run(w, "techlead", "clarify", n, input={"questions": ["How?"]})
    assert tl.result["decision"] == "answer"
    assert any("Rehearsal: go ahead." in m for m in await state.recent_memories("shop"))


async def test_a_stale_review_is_skipped(worker):
    w, gh, proxy, origin = worker
    n = gh.add_issue()
    sh(origin, "git", "update-ref", "refs/pull/9/head", sh(origin, "git", "rev-parse", "main"))
    job = await claim_and_run(w, "reviewer", "review", n, pr=9, head_sha="0" * 40)
    assert job.result["outcome"] == "stale" and gh.posted_reviews == [] and proxy.issued == []
