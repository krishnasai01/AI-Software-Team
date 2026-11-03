# AI Software Team: Implementation Plan

Nov 1, 2026 · @Krishna · Revised Nov 2, 2026: autonomy-first, no Temporal (details in `docs/design-v2.md`)

## Summary and goals

We are building an always-on AI software team: six independent Claude-powered agents that plan, build, review, test and ship a product, coordinated through GitHub and supervised by you. Target: Phase 1 (an engineer and a reviewer shipping real PRs) in about 4 weeks, the full team in about 10, and an MIT open-source release of the platform around week 12.

**Success criteria by the end of Phase 3**

- Agents pick up ready issues and open passing PRs around the clock, without prompting.
- Any single agent, worker or API outage pauses only that part, and work resumes on its own.
- You spend under 10 minutes a day, only on decisions that need a person: anything that expands the agents' own powers, high-risk or irreversible changes, product direction, and questions the tech lead agent can't answer.
- Every run is traceable (transcript, logs, cost), and every agent stays within a daily budget.

**Guiding principles**

- Agents coordinate through shared state (GitHub and Postgres), never by calling each other.
- Pay for intelligence (Anthropic API), use free tools for collaboration (GitHub, Slack or Discord), self-host the rest.
- Humans gate anything irreversible or high-risk, and anything that expands the agents' own powers. Everything else is verified automatically instead of approved by hand.
- Autonomy is earned: every product starts in shadow mode, is promoted on its track record with your approval, and is demoted automatically after a bad merge.
- Wrap every vendor in a thin interface so any layer can be swapped with one file.
- Agent-written code only ever runs inside a sandbox.

## Final stack

The only meaningful costs are Claude API tokens, two small servers, and optionally your own Claude subscription; everything else is free or self-hosted.

| Layer | Choice | Cost | Exit path if it changes |
| --- | --- | --- | --- |
| Agent brains | Anthropic API: Opus 5.5 (engineer, tech lead), Sonnet 5.5 (reviewer, QA, PM, DevOps), Haiku 5.5 (triage, digests, checks) | Paid, main cost | Route roles to open-weight models through a gateway |
| Agent harness | Claude Agent SDK, one process per run | Free | Swap behind the `run_agent()` interface |
| Your own coding | Claude Code on a Pro or Max plan | Optional | n/a |
| Code, CI, board | GitHub repos, Issues, Projects, Actions | Free | Forgejo; export issues via API |
| Agent identities | One GitHub App per agent role | Free | Forgejo bot accounts |
| Orchestration | A level-triggered reconciler over GitHub state, and a Postgres job queue (leases, heartbeats) | Free | Temporal, if workflows outgrow a state machine |
| State and memory | Postgres 16 + pgvector | Free | Any Postgres host |
| Sandboxes | Docker container per task, gVisor runtime | Free | Hosted sandboxes behind the `Sandbox` interface |
| Servers | 2 Linux servers from any provider or region, Docker Compose; k3s later | Small fixed | Any VPS or cloud |
| Chat and approvals | Slack, free tier | Free | Mattermost via the `Chat` adapter |
| Agent tracing | Langfuse, self-hosted | Free | OpenTelemetry to any backend |
| Infra monitoring | Prometheus, Grafana, Loki | Free | n/a |
| Product errors | Sentry free tier | Free | GlitchTip (self-hosted) |
| Secrets | SOPS-encrypted files (Infisical later) | Free | n/a |
| Model gateway | LiteLLM: holds the key, mints per-run keys, enforces per-role daily budgets and the team ceiling | Free | n/a |

## System architecture

The system runs on two servers. The control server holds shared state and workflows; the worker server runs agents and their sandboxes.

&#91;embedded content: System architecture · 2 servers, 3 shared systems\]

GitHub is the source of truth. Webhooks only nudge the reconciler, which reads each ticket's current state (issue, PR, CI, reviews, job history), decides the next step with one pure function, and acts: it queues agent jobs, posts the merge-policy check, turns on auto-merge, asks you a question, or reverts a bad merge. Workers claim jobs from Postgres and run each in its own sandbox. The sandbox gets the repo as a git bundle and holds no GitHub credentials; the worker verifies its commits in a clean repository before pushing. Claude is reached only through the LiteLLM proxy. Approvals and questions reach you in chat and in one daily digest.

## Repository layout

Two kinds of repo: one `agent-company` monorepo that runs the team, and each product repo, which carries its own product brain. Paths assume Python, the chosen language.

**The agent-company monorepo**

```
agent-company/
  core/                 thin interfaces; the only place vendor SDKs are imported
    model.py            ask_model(), run_agent() -> Claude Agent SDK
    github.py           GitHub Issues, PRs, checks, rulesets
    sandbox.py          Sandbox interface + Docker implementation
    gitops.py           trusted git work on the worker (bundles, verification, push)
    chat.py             Slack or Discord adapter
    memory.py           pgvector memory
    budget.py           per-agent spend tracking and caps
  agents/
    pm/ techlead/ engineer/ reviewer/ qa/ devops/
      prompt.md         system prompt
      config.yaml       model, tools, MCP servers, turn and budget limits
      hooks.py          pre-tool-use checks
  orchestrator/
    reconciler.py       the loop: nudges, sweeps, daily digest and promotions
    decide.py           the ticket lifecycle as one pure function
    policy.py           which PRs may merge without a human
    worker.py           job runner: bundles in, sandbox, verify, push
    jobs.py             Postgres job queue
    webhooks/           GitHub webhook receiver (stores nudges)
  sandbox-images/       Dockerfiles per product stack
  db/migrations/
  infra/                docker-compose.yml, Caddy, backups, SOPS secrets
  dashboards/           Grafana JSON
  templates/product-brain/   brain, CLAUDE.md, merge policy, CODEOWNERS for a product
```

**Each product repo**

```
<product>/
  CLAUDE.md             build and test commands, conventions; imports docs/brain/*
  docs/brain/
    product-brief.md    what it is, who it's for, business model
    architecture.md     components, data, key flows
    roadmap.md          epics in priority order
    conventions.md      code style, testing rules, definition of done
    decisions-log.md    dated decisions and why
  .agent-company/
    policy.yaml         which changes may merge without you (you own it)
  .github/              CI workflows, CODEOWNERS (sensitive paths only), PR template
```

Every agent reads the product brain before working, so all agents stay on the same page. Because it rarely changes, it is also the ideal block for prompt caching.

## Agent roster

Six role agents plus a non-LLM reconciler; each runs on its own, with its own GitHub App, tools and daily budget. Caps are generous on purpose and sit inside the team-wide ceiling ($250 a day, $500 in Phase 3).

| Agent | Triggered by | Model | Main tools | GitHub permissions | Produces | Daily cap |
| --- | --- | --- | --- | --- | --- | --- |
| PM | Daily schedule; new feedback or Sentry issues | Sonnet 5.5 | GitHub Issues, Sentry, analytics | Issues: write | Epics with acceptance criteria; roadmap updates via PR | $10 |
| Tech lead (Phase 1) | Epic labeled `needs-breakdown`; any escalation: a blocked or failing engineer, or 3 rounds of disagreement | Opus 5.5 | Repo read, Issues | Issues: write; contents: read | Sub-tickets sized to about a day, answers, rulings; asks you only what it can't decide | $20 |
| Engineer (pool of 1 to N) | Issue labeled `agent-ready` by a trusted person or the tech lead | Opus 5.5 | Sandbox shell, git, test runners | Contents: write and PRs: write, used only by the worker; the sandbox holds no token | Commits (the worker verifies and pushes them) and a PR summary | $50 each |
| Reviewer | New PR head with CI finished | Sonnet 5.5 | PR diff, repo read, CI results | PR reviews only; no contents write | Approve or request changes, plus a risk rating that can only make merging stricter | $15 |
| QA | Reviewer approved and CI green | Sonnet 5.5 | Sandbox, Playwright, test runners | Checks: write; Issues: write | Test report; bug issues; `qa-passed` label | $20 |
| DevOps/SRE (Phase 3) | Merge to main; Sentry alerts | Sonnet 5.5 | CI, deploy scripts, logs, Sentry | Actions and deployments: write | Staging deploys; low-risk prod deploys behind a canary with auto-rollback, high-risk ones after your approval; incident triage | $15 |
| Reconciler (not an LLM) | Webhook nudges, plus a sweep every 5 minutes | None | Postgres, GitHub, Slack | Issues, checks, auto-merge, reverts (orchestrator App) | Next step for every ticket, merge policy, reverts, pauses, daily digest | $0 |

Each agent's `prompt.md` states its job, its definition of done, what it must never do, and when to stop and ask. Agents only see the tools their role needs.

## Ticket lifecycle and coordination

Labels and pull requests are the team's shared state; the reconciler reads them and decides each next step. (A GitHub Projects board can mirror the state for you, but nothing triggers from it.)

&#91;embedded content: Ticket lifecycle · 7 board columns, 1 human gate\]

Agents never message each other. Rework, failed CI and conflicts send the ticket back to the engineer automatically; a lapsed lease returns the job to the queue; a blocked or failing engineer and three rounds of disagreement go to the tech lead agent. Only what the tech lead can't decide becomes a question for you (`needs-human`). Labels: `agent-ready`, `needs-breakdown`, `epic` and `split` (parents of sub-tickets), `needs-human`, `agent-review` (ask for a review of a PR an outsider wrote), `agent-revert`.

## Data model

GitHub is the record humans see and the source of truth for ticket state; Postgres holds what GitHub can't: jobs, runs, money, decisions and autonomy.

| Table | Holds |
| --- | --- |
| `products` | Each product: repo, sandbox image, default branch |
| `agents` | Each role: model, enabled, daily cap, LiteLLM team |
| `jobs` | The queue: one row per agent job, with lease and heartbeat; one live job per ticket |
| `runs` | One row per agent session: tokens, cost, outcome, trace link |
| `spend_daily` | Reporting copy of spend per role per day (LiteLLM enforces) |
| `tickets` | What the reconciler remembers per ticket: who labeled it, outsider origin, parent, CI bookkeeping |
| `policy_decisions` | The merge tier for each PR head, its reasons, and the shadow verdict one level up |
| `merges` | Every agent merge, watched afterwards: the track record behind promotion and demotion |
| `autonomy` | Each product's level (0–3) and whether auto-merge is paused |
| `questions` | Questions for you, with options, a default, and a deadline when the answer is reversible |
| `approvals`, `memories`, `events_inbox`, `incidents`, `controls` | Approvals; answers and decisions agents reuse; webhook nudges; what the system did on its own; kill switch and ceiling |

**Jobs use leases.** A worker claims a job with a 20-minute lease and renews it by heartbeat. If the worker dies, the lease lapses and the job returns to the queue; a crashed run is retried twice with backoff.

## Reconciler and event flow

Every step is decided from current state, not from events, so a missed, late, duplicated or forged webhook can delay work but never put a ticket in the wrong state.

1. GitHub sends a webhook. The receiver verifies its signature, stores it (de-duplicated by delivery id), and replies immediately. It needs only Postgres.
2. Within seconds, the reconciler reconciles the tickets the event mentions; every 5 minutes it sweeps every product as a safety net.
3. For each ticket it builds a snapshot and runs `decide()`, the whole lifecycle as one pure function (see `docs/design-v2.md`): queue the engineer, wait for CI, re-run flaky CI once, send failures back to the engineer, queue the reviewer, rework up to 3 rounds, then tech lead arbitration, evaluate the merge policy, and turn on auto-merge or ask you.
4. Sweeps close out merges, watch main afterwards, revert bad auto-merges and demote autonomy, read your `/answer` comments, close finished epics, verify the repo's protection settings, and pause agents on anomalies.
5. Once a day: the digest, promotion proposals, and the LiteLLM budget sync.

## Agent runtime and sandboxes

Every agent run is a fresh, disposable Docker container holding the repo, the toolchain and the Claude Agent SDK; the worker starts it, collects a structured result, and destroys it.

**One run, start to finish**

1. A worker claims the job, checks the budget, and fetches the repo into a clean workspace of its own with the role's GitHub App token.
2. It packs the base branch and the work branch into a git bundle, and writes a task file: the ticket (marked as untrusted input), the repository's `CLAUDE.md` and product brain *from the base branch*, relevant memories, and a per-run model key with a hard budget. No GitHub token goes in; the real Anthropic key never does.
3. It creates a container from the product's image under gVisor (2 vCPU, 4 GB, no host mounts) on a network of its own that reaches only the model proxy and the egress allowlist, copies the task in, and starts it.
4. Inside, the runner clones from the bundle and runs the Claude Agent SDK: Claude Code's own system prompt plus the role prompt and project instructions, the role's tools, pre-tool-use hints, and hard limits on turns and spend.
5. The agent commits; the runner writes a result file and a bundle of the new commits. The worker copies them out and destroys the container. A 90-minute wall-clock timeout, or a lost job lease, kills anything still running.
6. The worker verifies the commits in its own repository (they must build on the branch and touch no protected path), then pushes and opens or updates the PR. Reviews and tech lead decisions are posted the same way, from outside the sandbox.

**Isolation and capacity**

- Each sandbox gets its own internal network with only the model proxy and the egress allowlist proxy (package registries, GitHub) attached: no route to the internet or to other sandboxes.
- The model proxy is LiteLLM on the worker server. It holds the Anthropic key, enforces per-run, per-role and team-wide budgets on every request, and logs cost.
- One prebuilt image per product stack, with dependencies and the Claude Code CLI installed, rebuilt weekly.
- A worker server with 8 vCPU and 32 GB runs about 4 to 6 sandboxes at once. Add workers to add engineers.

## Guardrails and security

Agents can propose anything but cannot make an irreversible or high-risk change alone, and cannot expand their own powers; GitHub settings, credentials and code outside the sandbox enforce that, not prompts.

**GitHub**

- A ruleset on `main`: PR required, CI and the `agent-company/policy` check must pass (pinned to the orchestrator App so nothing else can set it), code-owner review required, stale approvals dismissed, approval of the latest push required, branches up to date or a merge queue, no force pushes, no bypass. The reconciler re-checks this every few minutes and falls back to "you merge everything" if it drifts.
- `CODEOWNERS` lists only the sensitive paths (CI, infra, auth, payments, migrations, the brain, the merge policy), so those always need you while low-risk changes can merge on their own once earned.
- The merge policy (`.agent-company/policy.yaml`, read from the base branch) decides which PRs need you; deterministic rules set the floor and the reviewer's opinion can only raise it.
- One GitHub App per role with least privilege; tokens never enter a sandbox.
- Only issues labeled by people on the trusted list, or split out by the tech lead, become agent work; work that started from outsider input always needs your approval to merge. Outsiders' PRs are reviewed only when a trusted person asks.

**Agent behavior**

- The worker rejects commits that rewrite history or touch protected paths (CI, infra, secrets, `CLAUDE.md`, the brain, the merge policy), checked in its own repository; pre-tool-use hooks warn the agent early.
- Hard caps on turns and spend per run, plus a daily cap per agent.
- Issue text, PR comments, web pages and dependency docs are data, not instructions. Prompts say so, and any request for credentials or policy changes stops the run and alerts you.
- Agents cannot edit their own prompts, configs, policy, autonomy level or budgets. They may lower their own autonomy (pauses, demotions); only you can raise it.
- Kill switch: `orchestrator.admin pause` stops all new work. Agents also pause themselves on a spend spike, three failed runs in a row, a suspected prompt injection, or a bad auto-merge.

**Secrets and servers**

- Secrets are SOPS-encrypted in the repo and decrypted only on the servers. No production credentials ever enter a sandbox.
- GitHub tokens expire within an hour; rotate the Anthropic key quarterly.
- SSH keys only, a host firewall, and admin UIs (Grafana, Langfuse) reachable only over a private network such as Tailscale or WireGuard.
- Daily encrypted Postgres backups stored off the servers, with a restore test every month.
- Always-on agents use an Anthropic API key, not a personal subscription login.

## Observability, cost control and failure handling

Two numbers tell you whether the team is working: cost per merged PR, and time from **Ready** to merged. Everything below exists to keep those visible and bounded.

**Observability**

- Langfuse records every run: prompts, tool calls, tokens and cost. Each PR links to its runs.
- Grafana dashboards show tickets per board column, runs per hour, success rate, cost per merged PR, spend against cap per agent, and sandbox CPU and memory.
- Chat alerts fire when an agent pauses at its cap, a ticket sits for 24 hours, 3 runs fail in a row, API errors spike, disk passes 80%, or a backup fails.

**Cost control**

- Model by role: Opus only for engineering and breakdown; Sonnet and Haiku for the rest.
- Prompt caching for the product brain and role prompts, which every run repeats.
- Small tickets, about a day of work each; large tickets burn tokens on re-reading context.
- Caps per run and per agent per day under a team-wide ceiling of $250 a day ($500 in Phase 3), plus a daily spend line in the digest.

**What happens when something fails**

| Failure | Detected by | Automatic response |
| --- | --- | --- |
| An agent run crashes | No result from the sandbox | Retried twice with backoff, then the tech lead re-scopes or splits the ticket; you only if that fails too |
| An agent loops without progress | Turn or spend cap | The run stops; the tech lead splits the ticket |
| A worker server goes down | Heartbeats stop | Leases lapse, jobs return to the queue, other workers continue; leftover sandboxes are removed on restart |
| The control server goes down | External uptime check | Workers finish their jobs; the reconciler re-reads GitHub on restart, so nothing is lost |
| Anthropic API errors or rate limits | 429 and 5xx responses | Backoff and retry; after 30 minutes new runs pause and you are alerted; resumes on its own |
| A webhook is missed | Reconciler | Picked up within 5 minutes |
| An agent hits its daily cap | LiteLLM and the budget check | Budget shifts from idle roles within the ceiling; otherwise work waits until tomorrow. Only you raise the ceiling |
| Engineer and reviewer disagree 3 times | `decide()` | The tech lead arbitrates; you only if it flags a product or security call |
| CI fails, or the branch conflicts | `decide()` | Failed jobs re-run once, then the engineer fixes it with the logs; conflicts go back to the engineer |
| An auto-merged PR breaks main | Watching CI on main after the merge | Revert PR, merged automatically once green; ticket reopened; autonomy demoted and auto-merge paused |

In Phases 1 and 2 the control server is a single machine. That is acceptable because all state is persisted and backed up; a standby replica is a Phase 3 item.

## Phased roadmap

Five phases over about 12 weeks; each phase starts only when the previous gate is met, and the last one publishes the platform under MIT.

&#91;embedded content: Roadmap · 5 phases, 4 gates, about 12 weeks\]

Week counts are targets, not commitments; the gates decide when to move on.

**Phase 0: Foundations (week 1)**

- [ ] Pick the first product and a server provider
- [ ] Create the `agent-company` repo; provision 2 VPS with a private network, firewall and SSH keys
- [ ] Bring up the Compose stacks: Postgres with pgvector, the reconciler, the webhook receiver and Caddy; LiteLLM, the egress proxy and the job runner
- [ ] Create the GitHub Apps (orchestrator, engineer, reviewer); add the ruleset, `CODEOWNERS` and merge policy to the first product
- [ ] Set up SOPS secrets and daily off-site backups, and test a restore
- [ ] Write the first product brain

**Phase 1: Engineer + reviewer (weeks 2 to 4)**

- [x] Build the `core/` interfaces: model, GitHub, sandbox, git operations, chat, budget
- [x] Build the sandbox images (base, Python, Node, rehearsal), with gVisor and per-job networks
- [x] Write the engineer, reviewer and tech lead prompts, configs and hooks
- [x] Build the webhook receiver, the reconciler with `decide()`, the job queue and the worker
- [x] Build the merge policy, earned autonomy (shadow mode, promotion, demotion), auto-revert, questions, the digest, the kill switch and automatic pauses
- [ ] Rehearse end to end on a scratch repo with the scripted agent ($0)
- [ ] Pilot on 10 small tickets in shadow mode and tune prompts from the failures

**Phase 2: Full product loop (weeks 5 to 7)**

- [ ] Add the PM and QA agents; QA becomes a merge requirement for user-facing changes
- [ ] Add Slack buttons for approvals and answers, and weekly retro proposals
- [ ] Give QA preview builds and Playwright
- [ ] Add a dependency license check, so dependency updates can be low risk
- [ ] Build Grafana dashboards and alerts

**Phase 3: Scale and harden (weeks 8 to 10)**

- [ ] Add the DevOps/SRE agent: canary deploys with auto-rollback (autonomy level 3)
- [ ] Add a second worker server and grow the engineer pool to 2 to 4
- [ ] Onboard the second product
- [ ] Add a Postgres standby replica, run a restore drill, and complete a security review

**Phase 4: Open-source release (weeks 11 and 12)**

- [ ] Complete the release checklist in the next section
- [ ] Tag v0.1.0 and announce the release

## Open-source release (MIT)

The `agent-company` platform will be published under the MIT license once Phase 3 is stable; products, deployment config and secrets stay private. MIT gives the widest adoption and the least friction; the tradeoff is that anyone may build a closed or hosted version, and it carries no explicit patent grant (Apache 2.0 would add one).

| Published (MIT) | Stays private |
| --- | --- |
| `core/`, `orchestrator/`, `agents/` role definitions, `sandbox-images/` | Real secrets, `.env` files, server addresses |
| `infra/` Compose files with example config, `dashboards/` | Your products' repos and product brains, unless you choose otherwise |
| `templates/product-brain/`, docs, examples | Production budgets, run history, any prompts you treat as your edge |

**Dependencies keep their own licenses.** We depend on them rather than copy them in, so the platform can be MIT.

| Dependency | Its license | How we use it | Note |
| --- | --- | --- | --- |
| Claude Agent SDK | Python repo MIT; use governed by Anthropic's Commercial Terms | Library dependency | Users bring their own API key; no claude.ai login; own branding, not presented as an Anthropic product |
| LiteLLM | MIT core; enterprise folder licensed separately | Separate container | Core features only |
| Langfuse | MIT core; enterprise folder licensed separately | Separate container | Core features only |
| Postgres | PostgreSQL License | Separate container | None |
| gVisor | Apache 2.0 | Container runtime | None |
| Grafana, Loki | AGPL-3.0 | Unmodified separate containers | Never bundle or modify them inside our code |

**Release checklist**

- [ ] Confirm the copyright holder (you or the company) and that every contributor has assigned their IP to it
- [ ] Add `LICENSE` with the MIT text and `Copyright (c) 2026 <holder>`
- [ ] Add `THIRD_PARTY_LICENSES` listing every dependency and its license
- [ ] Scan the full history with gitleaks and trufflehog, then publish from a fresh repo with clean history
- [ ] Remove internal URLs, server IPs, product brains and real budgets; ship a `.env.example`
- [ ] Make defaults safe: approval gates on, low budgets, sandboxes required
- [ ] Write the README: what it is, a 10-minute Docker Compose quickstart, bring your own Anthropic API key, a token-cost warning, and a note that the project is not affiliated with Anthropic
- [ ] Add `SECURITY.md`, `CONTRIBUTING.md` (DCO sign-off on commits), `CODE_OF_CONDUCT.md`, and issue and PR templates
- [ ] Add public CI: tests, secret scanning, and a license check on new dependencies
- [ ] Publish v0.1.0 as a GitHub Release, with images on GitHub Container Registry and docs on GitHub Pages
- [ ] Get a lawyer's review of the license, IP ownership, and the copyright status of AI-generated code

Outside contributions are accepted under a DCO. If you might ever relicense the project or sell a commercial edition, switch to a CLA before accepting the first outside contribution.

## Risks, decisions and next steps

The biggest risks are token cost and PR quality, and both are managed the same way: small tickets, strong tests, and tracking cost per merged PR from week one.

| Risk | Mitigation |
| --- | --- |
| Token spend runs above plan | Per-run and daily caps, model by role, prompt caching, cost per merged PR on the dashboard |
| PR quality is inconsistent | Strong CI, reviewer plus QA, the product brain, small well-specified tickets, lessons saved to memory |
| Prompt injection through issues or dependencies | Untrusted-input rules, no GitHub token in sandboxes, per-job networks, instructions from the base branch only, outsider-origin work always needs you |
| Your reviews become the bottleneck | Earned auto-merge for low-risk changes, the tech lead before you, questions batched in one digest |
| Auto-merged code hides a mistake | Deterministic floors, sensitive paths always need you, earned levels with size limits, watching main after merges, auto-revert and demotion |
| Single control server | Daily off-site backups, monthly restore drill, standby replica in Phase 3 |
| Vendor pricing or terms change | Thin interfaces; LiteLLM already in the path to reroute models |
| Building the platform crowds out product work | Do not start a phase until the previous phase meets its exit criteria |

**Decisions (closed November 2, 2026)**

- [x] **Language: Python.** It has a mature Claude Agent SDK package, plus the data tooling the memory and dashboards need.
- [x] **Chat for approvals: Slack** on the free tier, behind the `Chat` adapter so another tool can be swapped in later.
- [x] **Servers: any provider and region you prefer.** The plan only needs two Linux servers with Docker: a control server with about 4 vCPU and 16 GB, and a worker server with about 8 vCPU and 32 GB.
- [x] **First product: any.** The platform is product-agnostic; onboarding a product means writing its product brain and a sandbox image for its stack.
- [x] **Spend ceiling: $250 a day across all agents in Phases 1 and 2** (at most about $7,500 a month), **raised to $500 a day in Phase 3** for the larger engineer pool. Slack alerts at 70%; at 100% agents pause and you can raise the ceiling with one tap. It is a ceiling, not a forecast: use the first two weeks of real spend to tune it.
- [x] **Orchestration: a level-triggered reconciler and a Postgres job queue instead of Temporal** (review of Nov 2): GitHub stays the only source of truth, so missed or forged events can't corrupt a ticket.
- [x] **Auto-merge: earned.** Shadow mode first; promotion on the track record with your approval; automatic demotion.
- [x] **Silence as consent: only for reversible questions**, after 24 hours; never for merges or deploys.
- [x] **Production deploys (Phase 3): automatic for low-risk changes** behind a canary with auto-rollback; you approve high-risk ones.

**Next steps this week**

1. Pick the first product and a server provider; any choice works.
2. Create the `agent-company` repo, two VPS servers and the private network.
3. Write the product brain for the first product. Claude can draft it from the repo for your edits.
4. Follow `docs/setup.md`: GitHub Apps, rulesets, the merge policy, both Compose stacks, then the $0 rehearsal on a scratch repo before the pilot.
