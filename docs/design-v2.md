# Agent Company v2: autonomy-first design

Nov 2, 2026 · Approved with the recommended decisions; milestone 1 implemented

This revises `AI Software Team Implementation Plan.md` in two ways:

- It applies the restructure from the code review: trusted steps move out of the sandbox, a
  level-triggered reconciler replaces Temporal, the model proxy enforces budgets, and a no-cost
  rehearsal harness tests the whole pipeline.
- It makes autonomy the default. The team merges its own work, recovers from failures and settles
  its own disagreements. You are asked only for the decisions under "Where you stay in the loop".

The plan and `CLAUDE.md` now match this document; it supersedes the earlier `docs/plan-review.md`.
"Implementation notes" at the end records where the build differs from this design's first draft.

## Goal

- Typical human time: under 10 minutes a day (the plan's target was 30), spent on a short list of
  decisions only a person should make.
- Nothing irreversible happens without you, and neither does anything that expands the agents'
  own powers.
- Every other decision is made by an agent or a deterministic check, recorded with its reasons,
  and reversible.

## Principles

1. **Reversibility sets the gate, not the role.** If a mistake can be detected and undone
   automatically, agents act. If it can't, you decide.
2. **Verification replaces approval.** Wherever an objective check exists, it decides instead of a
   person. That includes tests, CI, QA, policy rules and canary metrics.
3. **Escalate to an agent before a person.** Ambiguity, failures and disagreements go to the tech
   lead agent first. You see only what it can't resolve.
4. **Batch what doesn't need to block.** Questions arrive together in the daily digest. One parked
   ticket never stops the others.
5. **Autonomy can drop automatically; only you can raise it.** Agents reduce their own autonomy
   by themselves (demotion, pause). Only you can increase it.
6. **Deterministic rules set the floor; model judgment can only raise risk.** No model output can
   lower a risk tier, so a prompt injection can't talk its way past a gate.
7. **Fail closed.** If a check, the policy engine or the GitHub configuration can't be verified, the
   work waits for you.

## Where you stay in the loop

| You decide | Why it isn't automated | How it reaches you |
| --- | --- | --- |
| Changes to the agents' own powers: prompts, configs, hooks, policy rules, autonomy level, spend ceiling, App permissions, CI, infra, rulesets, CODEOWNERS, the product brain | An agent that can change its own limits has no limits. The brain is part of every agent's system prompt. | PR to a code-owned path, or a one-tap approval (promotions, ceiling raises) |
| High-risk code: sensitive paths (auth, payments, data deletion, migrations, secrets handling, public APIs), new dependencies outside the license allowlist, work that started from outsider input | Automated reviewers can be fooled, and a miss is expensive | Approval request with a review brief: what changed in the sensitive area and what to check |
| Irreversible external actions: public releases and announcements, messages to customers, paid services or accounts, operations on production data | Can't be undone | Approval request |
| Production deploys that are high-risk, or whose canary results are unclear (Phase 3) | Blast radius | Approval request with the canary results |
| Product direction: new epics, roadmap priorities, the product brief, pricing | Business judgment | Roadmap PRs drafted by the PM agent (M2) |
| Questions only you can answer (business rules, credentials, accounts, legal) after the tech lead agent couldn't | The information isn't in the repo or the brain | Batched in the digest. The ticket parks and everything else continues. |
| Security events, and automatic fixes that failed | Needs judgment under uncertainty | Immediate Slack alert |

**Everything else is autonomous:**
- picking up and breaking down work, implementing, reviewing and reworking;
- CI fixes, rebases and flaky-test retries;
- settling disagreements;
- low-risk merges, once earned, and reverting bad auto-merges;
- retries and requeues;
- moving budget between roles within the ceiling;
- pausing on anomalies.

## How autonomy is kept safe

### Risk tiers and the policy engine

For every PR head, the orchestrator computes a tier and posts it as the `agent-company/policy`
check. It runs outside any sandbox, and it reads the policy file from the **base** branch, so a PR
can't edit its own rules. The file is `.agent-company/policy.yaml` in the product repo, and you are
its code owner. There are two tiers:

- **auto:** GitHub's native auto-merge is turned on. GitHub merges once CI and the policy check pass.
- **human:** you approve the PR, the policy check turns green and GitHub merges. You never click merge.

Deterministic inputs set the floor:
- changed paths compared with the sensitive list;
- diff size;
- new dependencies or lockfile changes;
- migrations;
- a drop in test coverage;
- missing CI;
- where the ticket came from (anything that started from outsider input is `human`);
- the product's autonomy level.

The reviewer and QA agents give a risk opinion, which can only raise the tier. An example
policy file:

```yaml
sensitive_paths: [".agent-company/**", ".github/**", "infra/**", "docs/brain/**",
                  "**/auth/**", "**/billing/**", "**/migrations/**"]
auto_merge:
  level_1: {max_changed_lines: 50, max_files: 10, only_paths: ["**/*.md", "docs/**", "tests/**"]}
  level_2: {max_changed_lines: 300, max_files: 15}
```

The other floors (dependency and migration changes, missing CI, outsider input, reviewer risk)
are built in and can't be switched off by the policy file; `orchestrator/policy.py` holds the
defaults. The template is `templates/product-brain/.agent-company/policy.yaml`.

GitHub enforces the result independently of our code:
- The `agent-company/policy` check is required and pinned to the orchestrator App, so no other
  token can forge it.
- CODEOWNERS lists only the sensitive paths, with "Require review from Code Owners" turned on. Those
  PRs need your approval even if the policy engine has a bug.
- These rules are also on: dismiss stale approvals, require approval of the latest push, required
  CI, branches must be up to date (or use a merge queue), and no one can bypass them.
- The reconciler re-reads this configuration on every sweep. Any drift drops the product to
  level 0 and alerts you.

### Earned autonomy levels

| Level | What auto-merges |
| --- | --- |
| 0 Shadow | Nothing. The policy engine records what it *would* have merged, and you merge. Default for every new product. |
| 1 Trivial | Docs, tests and tiny changes |
| 2 Low risk | Small changes outside sensitive paths |
| 3 Low risk + deploy | Level 2, plus production deploys behind a canary (Phase 3) |

Promotion to the next level is proposed automatically when the record supports it: 20 merges (or
shadow "auto" verdicts) at the current level with no reverts and no main-branch breaks, and 14 days
without incidents. You approve it with one tap.

Demotion is automatic. A revert, a main-branch CI break, or a production error linked to an
auto-merged PR drops the product one level and pauses auto-merge until you look.

### Escalation ladder (replaces `needs-human` stops)

| Situation | First | Then | You, only if |
| --- | --- | --- | --- |
| Engineer run crashes | Retry with backoff, up to 2 times | Tech lead re-scopes or splits the ticket | It still fails after the split |
| Engineer hits its turn or budget cap | Tech lead splits the ticket | Engineer resumes on the smaller pieces | Never |
| Ticket is ambiguous, or the engineer asks | Tech lead answers from the brain, the code and past decisions | The decision is saved as a memory | The answer needs information only you have |
| Reviewer requests changes 3 times | Tech lead arbitrates | Engineer reworks with the ruling, or the PR proceeds | The tech lead flags it as a product or security call |
| CI fails | Re-run the failed jobs once (flaky-test check) | Engineer fixes it using the CI logs | Repeated failures the tech lead can't resolve |
| PR conflicts with, or is behind, the base branch | Update the branch, or engineer rebases | None | Never |
| No progress for 24 hours | Requeue the stuck step | None | Never |
| An auto-merged PR breaks main | Revert PR, auto-merged | Ticket reopened with the failure; autonomy demoted | The revert itself fails |
| Daily cap reached | Move budget from idle roles, within the ceiling | Queue until tomorrow | You want to raise the ceiling |
| Model API outage | Back off, pause new runs, resume automatically | None | Never |

### Questions

When something does need you, the question includes the context, the options, the agent's
recommended default and the cost of waiting. Questions are batched in the morning digest. You
answer in Slack (with buttons from M2) or with an issue comment.

Each answer is saved as a memory with its source, so the same question isn't asked twice. The
weekly retro proposes moving recurring answers into the brain. That needs your approval, because
the brain is in every agent's system prompt.

### Kill switch and automatic pauses

One command stops all new work: `orchestrator.admin pause [product]`, later also a Slack command.
Agents also pause themselves on any of these:
- spend running at more than twice its usual hourly rate;
- three failed runs in a row for one role;
- a security event, such as a request for credentials or a prompt injection flagged by a guard or
  the reviewer;
- a revert of an auto-merged PR;
- drift in the GitHub configuration.

### Your day

You get one morning digest with:
- what shipped and what's in progress;
- spend against the ceiling;
- autonomy metrics: auto-merge rate, revert rate, cost per merged PR, lead time;
- a short "needs you" list: high-risk approvals with review briefs, questions with buttons, and
  promotion proposals.

On a typical day that's a handful of items.

## Architecture

### What changes from the current implementation

**A. Trusted steps leave the sandbox.**
- The worker fetches the repo with its own token and gives the sandbox a git bundle. The sandbox
  has no GitHub credentials at all.
- The sandbox returns its commits as a bundle, plus a result file.
- The worker verifies the commits in its own clean repository, against a base it fetched itself,
  with no hooks or config the agent could have touched. It then applies the protected-path policy
  and pushes as the engineer App.
- Project instructions (`CLAUDE.md` and the brain) come from the base branch through the task,
  with `setting_sources=[]`. They are appended to Claude Code's own system prompt
  (`preset: claude_code`) instead of replacing it.
- Each job gets its own internal network with only the model proxy and the egress proxy attached,
  plus a disk quota. Its container is killed if the worker restarts.

**B. A level-triggered reconciler and a Postgres job queue replace Temporal.**
- GitHub stays the single source of truth.
- Webhooks only nudge. The receiver stores them (it needs only Postgres) and wakes the reconciler
  for that ticket.
- The reconciler handles nudges within a few seconds (it reconciles just the tickets they name)
  and sweeps every product every 5 minutes. Either way it takes a snapshot of the ticket and a
  pure function, `decide(snapshot)`, returns the next action.
- Agent runs are rows in `jobs`, claimed with `FOR UPDATE SKIP LOCKED`. Each has a 20-minute
  lease, renewed by heartbeat.
- Temporal, its UI, and Valkey (which nothing uses) leave the stack.

**C. The model proxy enforces spending.** LiteLLM gets one team per role with a daily budget, plus
a global ceiling. Each run's key belongs to its role's team. `spend_daily` becomes a reporting copy.

**D. A no-cost rehearsal.** A scripted-agent sandbox image makes trivial commits on a scratch
repo. That tests GitHub Apps, webhooks, bundles, push, review, policy, auto-merge and revert without
spending tokens. It runs before the pilot, and in CI against a test repo.

```
 GitHub ── webhook ──► receiver ──► Postgres: events (nudges), jobs, questions, decisions, autonomy
   ▲                                     │
   │ labels, comments, checks,           ▼
   │ auto-merge, reverts        Reconciler (nudges in seconds, sweeps every 5 min)
   │◄─────────────────────────── snapshot(issue, PR, CI, reviews, jobs, policy, level)
   │                               → decide() → enqueue job | post policy check | enable auto-merge
   │                                            | ask a question | revert | pause | digest
   │                                     │ jobs (lease + heartbeat)
   │                                     ▼
   └── push as engineer App ◄── Worker: verify bundle in a clean repo ◄── SANDBOX (no GitHub token)
                                 bundle in ───────────────────────────────► agent works and commits
                                                                            model ─► LiteLLM (role budgets)
                                                                            packages ─► egress allowlist
```

### `decide()`: one ticket, the first matching rule wins

| # | Snapshot (always for the PR's current head) | Action |
| --- | --- | --- |
| 1 | Paused (kill switch, product paused, or ceiling reached) | Start nothing new |
| 2 | Issue closed, or `agent-ready` removed | Cancel queued jobs and stop |
| 3 | An open question for this ticket | Wait. At the deadline, apply the stated default if allowed (decision 2). |
| 4 | A job for this ticket is running | Wait |
| 5 | No PR yet | Engineer `implement`, or the escalation ladder if the last attempt failed or asked |
| 6 | PR merged | Close out and start watching main after the merge |
| 7 | PR closed without merging | Stop and note it in the digest |
| 8 | PR conflicts with, or is behind, the base branch | Update the branch (a clean update keeps the existing review), or engineer `rebase` |
| 9 | CI pending, or not started yet (10-minute grace) | Wait. No CI after the grace period means tier `human`. |
| 10 | CI failed | Re-run the failed jobs once, then engineer `fix-ci` with the logs |
| 11 | No review on this head | Reviewer |
| 12 | Changes requested on this head | Engineer `rework` (rounds 1–3), then tech lead `arbitrate` |
| 13 | Approved, QA required and not passed yet (M2) | QA |
| 14 | Approved (and QA passed) | Evaluate the policy and post the check. `auto` turns on auto-merge; `human` sends an approval request. |
| 15 | Waiting more than 24 hours on an agent step | Requeue that step |

A PR belongs to a ticket only if it comes from the same repo, is on `agent/issue-N`, and was
authored by the engineer App. Outsiders' PRs aren't reviewed automatically; a collaborator can
request a review with a label.

### Data model changes

- `jobs` replaces `claims`. Each job has a kind and mode, ticket, PR, head sha, status, attempt,
  `not_before`, lease and heartbeat, input, result, and run id. There is at most one active job
  per (ticket, kind).
- New tables:
  - `policy_decisions`: PR, head sha, tier, level, reasons, shadow flag.
  - `autonomy`: product, level, who or what changed it, and why.
  - `questions`: ticket, question, options, default, deadline, answer, who answered.
- Changed tables: `memories` gets source and status columns, `approvals` gets `promotion` and
  `policy` kinds, and `runs.workflow_id` becomes `job_id`.

## Roles by milestone

| Milestone | Agents | Autonomy |
| --- | --- | --- |
| M1 Pilot (built) | Engineer (Opus). Reviewer (Sonnet). Tech lead (Opus): breaks down epics, answers questions, arbitrates disagreements, splits tickets. The reconciler's digest. | Level 0 shadow; promotion to levels 1–2 with your approval; watching main after merges, with auto-revert and demotion; the escalation ladder; questions and memories; the kill switch and automatic pauses |
| M2 Full loop | Adds QA (Sonnet, Playwright) and PM (Sonnet): intake from feedback and Sentry, roadmap PRs | QA as a merge requirement for user-facing changes; Slack buttons; weekly retro proposals; a dependency license check, so dependency changes can be low risk |
| M3 Scale | Adds DevOps (Sonnet) | Level 3 (canary deploys with auto-rollback), a second worker, a Postgres standby |
| M4 | None new | Open-source release |

Moving the tech lead into M1 is what makes the escalation ladder possible. It adds Opus spend
for each escalation, but it replaces most stops that would otherwise wait for you.

## Review findings and where they're resolved

| Finding from the Nov 2 review | Resolved by |
| --- | --- |
| 1. Push bypass and token capture inside the sandbox | A: no token in the sandbox; the worker verifies and pushes |
| 2. A PR can rewrite its reviewer's instructions | A: instructions come from the base branch, with `setting_sources=[]` |
| 3. Outsider PRs spoof tickets and drain the review budget | B: strict PR-to-ticket matching; outsider PRs aren't reviewed automatically |
| 4. CI skipped; a CI failure after approval strands the ticket | `decide()` rules 9 and 10 |
| 5. Budget caps overshoot, and spend gets lost | C |
| 6. A result line over 64 KiB triggers paid retries | A: the result comes back as a file |
| 7. A custom system prompt replaces Claude Code's | A: `preset: claude_code` plus append |
| 8. Reviewer token permissions | A: sandboxes get no GitHub token |
| 9. The webhook receiver depends on Temporal | B: the receiver needs only Postgres |
| 10. Orphaned sandboxes | A: stale labeled containers are killed when the worker starts |
| 11. Workflow versioning | B: no Temporal |
| 12. LiteLLM `drop_params` and the auth header | C and D: Anthropic pass-through route and `ANTHROPIC_AUTH_TOKEN`, checked in the rehearsal |
| Minor findings | Fixed in M1: token caching, guard friction, the labeler check, the disk quota, traffic between sandboxes, repeated notifications, branch-protection verification |

## Remaining risks

- An LLM reviewer and QA can miss a subtle bug, or a deliberately disguised change, outside the
  sensitive paths. Earned levels, size limits, the outsider-input rule, watching main after merges,
  and auto-revert all reduce this, but they don't eliminate it. Keep the sensitive-path list generous.
- The policy is only as good as each product's sensitive-path list and test suite. Weak tests make
  level 2 unsafe, and the promotion rules can't detect that by themselves.
- More agent runs (tech lead, QA, retries) raise the cost per merged PR, in exchange for fewer stalls.
- To check in the rehearsal:
  - that GitHub rulesets enforce code-owner review when zero approvals are required;
  - LiteLLM's Anthropic pass-through route with per-run keys;
  - that team budgets reset daily.

## Decisions (closed Nov 2, 2026)

1. **Auto-merge: earned.** Every product starts in shadow mode and is promoted on its track
   record, with your approval. Demotion is automatic.
2. **Silence as consent: reversible questions only.** A stated default takes effect after
   24 hours only for questions the tech lead marks reversible, never for merges or deploys.
3. **Production deploys (M3): automatic for low-risk changes** behind a canary with
   auto-rollback; you approve the high-risk ones.

## Implementation notes (milestone 1)

Where the build differs from the first draft above, and why:

- **Sweep timing.** Webhook nudges are handled within one 5-second tick, and every product is
  swept every 5 minutes. Sweeping every product every 30 seconds would exceed GitHub's API rate
  limits once a few tickets are open.
- **Dependency changes always need you** in M1. The license allowlist needs a dependency
  analysis step (M2); until then, any manifest or lockfile change sets the floor.
- **No coverage rule yet.** Nothing reports coverage to the platform, so a coverage floor would
  either do nothing or block everything. It returns when CI publishes coverage.
- **Watching main, auto-revert, demotion and promotion moved into M1.** Earned autonomy is only
  safe with them, so a promotion approved during the pilot is covered from its first auto-merge.
  Promotions stop at level 2 until the DevOps agent (M3) can deploy behind a canary.
- **A clean branch update keeps its review.** When the reconciler asks GitHub to bring a PR up to
  date, the reviewer's verdict carries over only if the new head is that merge (its first parent
  is the reviewed head). Any other new commit gets a fresh review. Your own approval may still be
  dismissed by GitHub's "dismiss stale approvals" rule; a merge queue avoids that churn.
- **The tech lead splits tickets through the orchestrator App** (it creates the sub-issues), so
  M1 needs no separate tech lead App. Sub-tickets inherit the parent's outsider-origin flag.
- **LiteLLM budgets.** Each role is a LiteLLM team whose daily budget is synced to its effective
  cap (including what the broker lets it borrow), plus a global proxy ceiling. LiteLLM resets
  budgets 24 hours after creation rather than at local midnight; the platform's own check uses
  the local day, so the stricter of the two applies.
