# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is the `agent-company` platform: an always-on, autonomy-first team of Claude agents (engineer,
reviewer, tech lead; QA, PM and DevOps come later) coordinated through GitHub. Read
`docs/design-v2.md` before changing orchestration, the merge policy or the sandbox. It is the
design of record, including where humans stay in the loop and why. `AI Software Team
Implementation Plan.md` is the overall plan; `docs/setup.md` is the deployment runbook.

## Commands

    uv sync                                   # install (Python 3.12, see .python-version)
    uv run ruff check . && uv run ruff format --check .
    uv run pytest -q                          # pure tests; DB and Docker tests skip without their deps
    uv run pytest tests/test_decide.py -k arbitration -q   # a single test

Full suite (what CI runs):

    docker run -d --rm --name agentco-test-pg -p 127.0.0.1:55432:5432 -e POSTGRES_USER=agent \
      -e POSTGRES_PASSWORD=agent -e POSTGRES_DB=agent_test pgvector/pgvector:pg16
    docker build -f sandbox-images/base/Dockerfile -t agent-company/sandbox-base:test .
    docker build --build-arg BASE=agent-company/sandbox-base:test -t agent-company/sandbox-rehearsal:test \
      sandbox-images/rehearsal
    TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:55432/agent_test uv run pytest -q

Processes: `python -m db.migrate`, `python -m orchestrator.reconciler` (control server),
`python -m orchestrator.worker` (worker server), `uvicorn orchestrator.webhooks.app:app`.
Operator commands (kill switch, approvals, answers, levels): `python -m orchestrator.admin --help`.
Onboarding assistant (a generic engine plus per-team playbooks; see `docs/onboarding.md`):
`python -m onboarding software-team [--no-model | --status]`.

## Architecture

**Level-triggered, not event-driven.**
- GitHub is the source of truth. Webhooks are only nudges: the receiver stores them, and the
  reconciler (`orchestrator/reconciler.py`) reconciles the tickets they mention within seconds and
  sweeps every product every 5 minutes.
- Each reconcile is `snapshot.build()` (GitHub plus job history), then `decide()`, then
  `actions.apply()`.
- `decide.py` is the whole ticket lifecycle as one pure function: first matching rule wins, plus
  the escalation ladder (engineer → tech lead → you). Add behaviour there and table-test it in
  `tests/test_decide.py`.
- Every action must be safe to repeat, because the next sweep re-reads state.

**Jobs.**
- Agent runs are rows in `jobs` (`orchestrator/jobs.py`), with one live job per ticket, claimed with
  `SKIP LOCKED`, holding a 20-minute lease renewed by heartbeat.
- Crashes retry with backoff. A lost lease kills the sandbox.

**The sandbox boundary** (`orchestrator/worker.py`, `core/gitops.py`, `core/sandbox.py`,
`agents/runner.py`):
- The worker fetches the repo into its own clean bare repo and copies a git bundle and
  `task.json` into a fresh container. The container has no host mounts, no GitHub token, and a
  per-job internal network with only the `litellm` and `egress` proxies attached.
- The runner writes `/out/result.json` and `/out/result.bundle`.
- The worker treats all of `/out` as untrusted. It verifies the commits in its own repo (they must
  descend from the start commit and touch no `PROTECTED_GLOBS`), then pushes and opens the PR
  itself.
- Project instructions (`CLAUDE.md`, `docs/brain/`) come from the **base** branch via the task, with
  `setting_sources=[]`, appended to Claude Code's preset system prompt.

**Merge policy and autonomy** (`orchestrator/policy.py`, `orchestrator/sweeps.py`):
- The policy is pure. It reads the product's `.agent-company/policy.yaml` from the base branch.
  Deterministic floors force a human, and the reviewer's risk opinion can only raise the tier.
- The result is posted as the required `agent-company/policy` check. `auto` turns on GitHub
  auto-merge.
- Levels 0–3 are per product. Level 0 is shadow mode.
- Sweeps watch main after each merge, revert bad auto-merges, demote automatically, propose
  promotions (applied only by `admin approve`), verify the repo's ruleset (drift fails closed), and
  pause agents on anomalies.

**Identities.** `ctx.gh` is the orchestrator App (labels, issues, checks, auto-merge, reverts).
`ctx.as_role("engineer")` opens PRs, and `ctx.as_role("reviewer")` posts reviews. Tests inject
`tests/fakes.py`'s `FakeGitHub` for all of them.

**Onboarding** (`onboarding/`, `docs/onboarding.md`). It runs on the operator's machine, with the operator's powers.
- The engine (`engine.py`, `playbook.py`, `registry.py`) knows nothing about any team. A playbook
  (`onboarding/playbooks/<name>/playbook.yaml`, plus `pack.py` for its probes and actions) describes one.
  `software-team` is the one for this platform.
- A step is done only when its probes say so. An action runs only after the operator's own yes at the
  terminal, asked by the engine. The model (`chat.py`, through `core.model.ChatSession`) only steers,
  with nine tools, no shell and no files.
- Secrets are typed at hidden prompts or generated, passed to `sops` and servers on stdin, and scrubbed
  (`SecretStore.redact`) from everything the model, the log or the terminal sees. Never put one in argv.
- `pack.py` must stay in step with `infra/*/docker-compose.yml` (`ENV_KEYS`) and `docs/setup.md`;
  `tests/test_onboarding_pack.py` fails when the compose files need a variable onboarding does not write.

## Rules that shape all code

- Vendor SDKs and APIs (anthropic, claude_agent_sdk, slack_sdk, the GitHub API, LiteLLM admin) are
  used only in `core/`.
- Agents never call each other. Nothing an agent produces is trusted until the worker or
  reconciler has checked it outside the sandbox.
- Anything that expands what agents may do stays human: prompts, configs, hooks, the policy,
  autonomy promotions, caps and the ceiling, CI, infra. Automatic code may only *lower* autonomy
  (demote, pause, disable).
- `agents/` runs inside the sandbox. Keep `agents/_shared/guards.py` and the hooks stdlib-only.
  They are early hints, not the security boundary. Prompt, config or hook changes take effect only
  after rebuilding the sandbox images.
- Text from tickets, PRs, reviews and memories reaches agents wrapped as untrusted data
  (`runner.untrusted`). Keep it that way.
- Grafana and Loki (AGPL) run only as separate, unmodified containers. Never commit secrets; they
  are SOPS-encrypted under `infra/secrets/`.
