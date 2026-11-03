# Implementation plan review (Nov 2, 2026)

The plan holds up: shared-state coordination, stable workflow ids, leases, human gates and thin
vendor interfaces are the right bones. These are the gaps found while implementing Phase 0–1, and
what the code does about each. Fold them back into the plan when convenient.

## Changes the code makes to the plan

1. **Tickets are triggered by the `agent-ready` label, not the board column.** GitHub only
   delivers `projects_v2_item` webhooks for organization-owned projects, and the payload lacks the
   new column value. Labels work for user and org repos alike, carry the sender (for the
   "only you, PM or tech lead" rule), and are already in the plan. The Projects board is kept as a
   best-effort mirror (`Tickets.set_board_status`), and the reconciler polls labels.

2. **A seventh GitHub App: `orchestrator`.** Someone must add `needs-human`, comment, and move board
   items, but every role App is least-privilege and the supervisor is read-only. The orchestrator
   App (Issues: write, Projects: write, Checks: read, Pull requests: read) does this bookkeeping and
   never touches code.

3. **Merge approval happens on GitHub in Phase 1, not in Slack.** Branch protection already requires
   your code-owner review, and no agent App may merge. So `TicketWorkflow` posts "ready for your
   merge" to Slack, records an `approvals` row, reminds every 4 hours, and waits for the
   `pull_request.closed` (merged) event. Slack buttons arrive with Phase 2, as the plan already says.

4. **Claims have a `waiting` state.** A 20-minute lease is right while an engineer works, but a
   ticket waiting a day on review or your merge would look dead. While parked, the claim belongs to
   the live workflow and has no lease. The reconciler only releases an expired lease when the
   workflow itself is gone (otherwise Temporal's activity heartbeat timeout is already re-running
   the agent on another worker).

5. **Credentials enter the sandbox on stdin, not as environment variables.** Env vars show up in
   `docker inspect` and `/proc/*/environ`. The GitHub token stays in the runner's memory and reaches
   git through `-c http.extraheader`, so it is never written to `.git/config`. The agent does not push:
   the runner pushes after its own check that the final diff touches no protected path. That makes
   "never edit CI/infra/secrets" deterministic rather than hook-dependent.

6. **CI failure short-circuits review.** `ReviewWorkflow` waits for CI first. If CI fails, it
   requests changes without starting the reviewer agent, so it spends no tokens.

7. **Closing an issue is a signal, not a cancel.** `Closes #N` closes the issue in the same instant the
   PR merges. Cancelling would race the merge event and lose the bookkeeping. The workflow waits
   5 minutes for the merge event before treating the close as an abandon.

## Gaps the plan should still close

- **The egress proxy is missing from the stack table.** Implemented here as Squid on `sandbox-net`
  (an `internal` Docker network) with a domain allowlist. Add it to the Phase 0 checklist.
- **LiteLLM needs its own Postgres database** for virtual keys and spend. It now uses a `litellm`
  database on the control server's Postgres, which makes the worker depend on the control server's
  Postgres. That's fine at this scale; it's noted for the Phase 3 standby work.
- **The engineer pool cap.** "$50 each" is stored as one per-role cap. When the pool grows past 1,
  set the cap to 50 × pool size (`python -m orchestrator.admin set-cap engineer 100`), or split
  roles per engineer.
- **Langfuse v3 is heavy** (ClickHouse, Redis, MinIO). Deploy it from Langfuse's own Compose file
  rather than vendoring it. LiteLLM sends traces through its `langfuse` callback. **To verify on the
  first pilot run:** that the `langfuse_trace_id` header the runner sets (through
  `ANTHROPIC_CUSTOM_HEADERS`) groups a run's calls into one trace. If not, filter traces by key
  alias, which is the run id.
- **The docker socket.** The agents worker mounts the host's Docker socket to start sandboxes, which
  is root-equivalent on the worker host. Agent code never runs in that container, but if you want a
  smaller blast radius, a rootless Docker daemon or a small sandbox-launcher service is the next
  step.
- **Hooks are best-effort against shell tricks** (for example `python -c "open('.github/x','w')"`).
  The guarantees come from the runner's diff check, CODEOWNERS, branch protection and App
  permissions. Treat hook denials as early feedback to the agent, not as the security boundary.

## Not built yet (by phase)

- Phase 2: PM, tech lead, QA agents. `PlanningWorkflow`, `QAWorkflow`, `DigestWorkflow`, full
  `BudgetGuard` (approval to raise a cap), Slack buttons, `memories`, Grafana dashboards.
- Phase 3: DevOps agent, `DeployWorkflow`, second worker, Postgres standby.
- Phase 4: release checklist.

The schema already includes all nine tables, so later phases don't need a migration for them.
