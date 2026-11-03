# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is the `agent-company` platform: an always-on team of Claude agents coordinated through GitHub
and Temporal. `AI Software Team Implementation Plan.md` is the design. `docs/plan-review.md`
records where the code deliberately differs from it and why; read both before changing workflows,
claims or the sandbox. Phase 0–1 (engineer + reviewer) is implemented. PM, tech lead, QA, DevOps,
digests, Slack buttons and memories are Phase 2+.

## Commands

    uv sync                                   # install (Python 3.12, see .python-version)
    uv run ruff check . && uv run ruff format --check .
    uv run pytest -q                          # unit + Temporal workflow tests
    uv run pytest tests/test_workflows.py -k rework -q   # a single test

Some tests skip themselves unless their dependencies are present:

    # Postgres tests (the database is wiped):
    docker run -d --rm --name agentco-test-pg -p 127.0.0.1:55432:5432 -e POSTGRES_USER=agent \
      -e POSTGRES_PASSWORD=agent -e POSTGRES_DB=agent_test pgvector/pgvector:pg16
    TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:55432/agent_test uv run pytest -q
    # Real-Docker sandbox tests:
    docker build -f sandbox-images/base/Dockerfile -t agent-company/sandbox-base:test .

Run locally: `python -m db.migrate`, `python -m orchestrator.worker orchestration|agents`,
`uvicorn orchestrator.webhooks.app:app`. Operator commands: `python -m orchestrator.admin --help`.
Deployment: `docs/setup.md`.

## Architecture

**Event flow.** GitHub webhook → `orchestrator/webhooks/app.py` verifies the HMAC, inserts into
`events_inbox` (de-duplicated by delivery id), and replies 202. Then `router.py` starts or signals
a workflow by stable id (`ticket-<owner>-<repo>-<n>`, `review-<owner>-<repo>-<pr>-<sha12>`).
`ReconcilerWorkflow` runs every 5 minutes. It replays failed inbox events, starts `agent-ready`
issues that have no running workflow, reviews unreviewed agent PRs, and releases expired leases
whose workflow is gone.

**Workflows** (`orchestrator/workflows/`). They call activities **by string name**, so tests can
swap in fakes registered under the same names. Keep names in sync with
`orchestrator/activities/`. Workflow code must stay deterministic: no I/O, and imports go through
`workflow.unsafe.imports_passed_through()`.

**Two task queues.** `orchestration` (control server) runs the workflows and light GitHub, DB and
chat activities. `agent-runs` (worker server) runs only `run_agent`, with concurrency =
`MAX_SANDBOXES`.

**One agent run** (`activities/agent_run.py`):
1. Budget check.
2. Mint a repo-scoped GitHub token and a per-run LiteLLM key with a hard budget.
3. `docker run -i --rm` the product's sandbox image, with the task (credentials included) on stdin.
4. Inside the container, `agents/runner.py` clones the repo, runs the Claude Agent SDK through
   `core.model.run_agent`, and prints one `AGENT_RESULT <json>` line.
5. Revoke the key, then record the run and its spend.

An engineer **never pushes itself**: the runner pushes, after refusing any diff that touches
`PROTECTED_GLOBS`.

**Identities.** Each role has its own GitHub App. `Tickets("orchestrator")` does labels, comments
and the board. `Tickets("engineer")` opens PRs. `Tickets("reviewer")` posts reviews. Don't use one
role's App for another role's action.

## Rules that shape all code

- Vendor SDKs (anthropic, claude_agent_sdk, slack_sdk, GitHub HTTP) are imported only in `core/`.
- Agents never call each other. They coordinate only through labels (`agent-ready`, `needs-human`,
  …) and Postgres. Every terminal path of `TicketWorkflow` must either remove `agent-ready` or add
  `needs-human`, because the reconciler restarts any open `agent-ready` issue without them.
- `agents/` runs **inside the sandbox**. Keep `agents/_shared/guards.py` and the hooks stdlib-only.
  Prompt, config or hook changes take effect only after rebuilding the sandbox images.
- Ticket text, PR bodies and review comments reach agents wrapped as untrusted data
  (`runner.untrusted`). Keep it that way.
- Humans gate merges, deploys and budget raises. Nothing auto-approves.
- Caps live in the `agents` table plus `TEAM_DAILY_CEILING_USD`. Never let an agent change them.
- AGPL components (Grafana, Loki) run only as separate, unmodified containers. Never commit secrets:
  they're SOPS-encrypted under `infra/secrets/`.
