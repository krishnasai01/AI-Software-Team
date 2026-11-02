# AI Software Team: Implementation Plan

Nov 1, 2026 · @Krishna

## Summary and goals

We are building an always-on AI software team: six independent Claude-powered agents that plan, build, review, test and ship a product, coordinated through GitHub and supervised by you. Target: Phase 1 (an engineer and a reviewer shipping real PRs) in about 4 weeks, the full team in about 10, and an MIT open-source release of the platform around week 12.

**Success criteria by the end of Phase 3**

- Agents pick up ready issues and open passing PRs around the clock, without prompting.
- Any single agent, worker or API outage pauses only that part, and work resumes on its own.
- You spend under 30 minutes a day, mostly approving merges and deploys and answering questions.
- Every run is traceable (transcript, logs, cost), and every agent stays within a daily budget.

**Guiding principles**

- Agents coordinate through shared state (GitHub and Postgres), never by calling each other.
- Pay for intelligence (Anthropic API), use free tools for collaboration (GitHub, Slack or Discord), self-host the rest.
- Humans gate anything irreversible: merges to main, production deploys, anything public.
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
| Durable workflows | Temporal, self-hosted | Free | LangGraph + Postgres checkpointer |
| State and memory | Postgres 16 + pgvector, Valkey (Redis) | Free | Any Postgres host |
| Sandboxes | Docker container per task, gVisor runtime | Free | Hosted sandboxes behind the `Sandbox` interface |
| Servers | 2 Linux servers from any provider or region, Docker Compose; k3s later | Small fixed | Any VPS or cloud |
| Chat and approvals | Slack, free tier | Free | Mattermost via the `Chat` adapter |
| Agent tracing | Langfuse, self-hosted | Free | OpenTelemetry to any backend |
| Infra monitoring | Prometheus, Grafana, Loki | Free | n/a |
| Product errors | Sentry free tier | Free | GlitchTip (self-hosted) |
| Secrets | SOPS-encrypted files (Infisical later) | Free | n/a |
| Model gateway | LiteLLM as the key-holding model proxy, from Phase 1 | Free | n/a |

## System architecture

The system runs on two servers. The control server holds shared state and workflows; the worker server runs agents and their sandboxes.

&#91;embedded content: System architecture · 2 servers, 3 shared systems\]

GitHub events start Temporal workflows, which hand runs to agent workers through task queues. Each run executes in its own sandbox, calls Claude only through the LiteLLM proxy, and delivers its work back to GitHub as branches and PRs. Postgres records claims, runs and spend for every agent; approvals reach you in chat.

## Repository layout

Two kinds of repo: one `agent-company` monorepo that runs the team, and each product repo, which carries its own product brain. Paths assume Python, the chosen language.

**The agent-company monorepo**

```
agent-company/
  core/                 thin interfaces; the only place vendor SDKs are imported
    model.py            ask_model(), run_agent() -> Claude Agent SDK
    tickets.py          GitHub Issues + Projects
    sandbox.py          Sandbox interface + Docker implementation
    chat.py             Slack or Discord adapter
    memory.py           pgvector memory
    budget.py           per-agent spend tracking and caps
  agents/
    pm/ techlead/ engineer/ reviewer/ qa/ devops/
      prompt.md         system prompt
      config.yaml       model, tools, MCP servers, turn and budget limits
      hooks.py          pre-tool-use checks
  orchestrator/
    workflows/          Temporal workflows
    activities/         Temporal activities
    webhooks/           GitHub webhook receiver
    supervisor/         reconciler, health checks, daily digest
  sandbox-images/       Dockerfiles per product stack
  db/migrations/
  infra/                docker-compose.yml, Caddy, backups, SOPS secrets
  dashboards/           Grafana JSON
  templates/product-brain/
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
  .github/              CI workflows, CODEOWNERS, PR template
```

Every agent reads the product brain before working, so all agents stay on the same page. Because it rarely changes, it is also the ideal block for prompt caching.

## Agent roster

Six role agents plus a non-LLM supervisor; each runs on its own, with its own GitHub App, tools and daily budget. Caps are generous on purpose and sit inside the team-wide ceiling ($250 a day, $500 in Phase 3).

| Agent | Triggered by | Model | Main tools | GitHub permissions | Produces | Daily cap |
| --- | --- | --- | --- | --- | --- | --- |
| PM | Daily schedule; new feedback or Sentry issues | Sonnet 5.5 | GitHub Issues, Sentry, analytics | Issues: write | Epics with acceptance criteria; roadmap updates via PR | $10 |
| Tech lead | Epic labeled `needs-breakdown` | Opus 5.5 | Repo read, Issues | Issues: write; contents: read | Issues sized to about a day, design notes, `agent-ready` label | $20 |
| Engineer (pool of 1 to N) | Issue enters **Ready** | Opus 5.5 | Sandbox shell, git, test runners | Contents: write (unprotected branches); PRs: write | Branch + PR with tests and a summary | $50 each |
| Reviewer | PR opened or updated | Sonnet 5.5 | PR diff, repo read, CI results | PR reviews only; no contents write | Approve or request changes | $15 |
| QA | Reviewer approved and CI green | Sonnet 5.5 | Sandbox, Playwright, test runners | Checks: write; Issues: write | Test report; bug issues; `qa-passed` label | $20 |
| DevOps/SRE (Phase 3) | Merge to main; Sentry alerts | Sonnet 5.5 | CI, deploy scripts, logs, Sentry | Actions and deployments: write | Staging deploys; prod deploy after approval; incident triage | $15 |
| Supervisor | Every 5 minutes | Haiku 5.5 for summaries only | Postgres, Temporal, Slack | Read only | Requeues, restarts, alerts, daily digest | $2 |

Each agent's `prompt.md` states its job, its definition of done, what it must never do, and when to stop and ask. Agents only see the tools their role needs.

## Ticket lifecycle and coordination

The GitHub Projects board is the team's shared to-do list: each column is a state, and each agent watches only the column it owns.

&#91;embedded content: Ticket lifecycle · 7 board columns, 1 human gate\]

An agent moves a ticket by changing its column or label, never by messaging another agent. Rework and failed tests send the ticket back to the engineer; a lapsed lease returns it to **Ready**; three review rounds or any agent's request for help sends it to **Needs human**. Labels carry the rest: `agent-ready`, `needs-breakdown`, `qa-passed`, `needs-human`, and one `product:<name>` label per product.

## Data model

GitHub is the record humans see; Postgres is the record the system trusts for claims, runs, money and approvals. Nine tables cover Phase 1 to 3.

| Table | Holds | Key columns |
| --- | --- | --- |
| `products` | Each product the team works on | repo, project board id, sandbox image, default branch |
| `agents` | Each agent role and its settings | role, model, enabled, daily cap (USD), GitHub App id |
| `claims` | Who is working on which ticket | ticket, agent, lease expires at, last heartbeat, status; at most one active claim per ticket |
| `runs` | One row per agent session | agent, ticket, workflow id, start, end, outcome, turns, tokens in/out, cost (USD), Langfuse trace link |
| `spend_daily` | Spend per agent per day | agent, date, spent, cap |
| `approvals` | Requests waiting on you | kind (merge, deploy, publish, budget raise), target, status, decided by, decided at, chat message link |
| `memories` | Lessons, pitfalls and decisions agents learn | product, kind, text, embedding (pgvector), source run |
| `events_inbox` | Every webhook received | delivery id (unique, for de-duplication), payload, processed at |
| `incidents` | Failures the supervisor acted on | kind, agent, ticket, action taken, resolved at |

**Claims use leases.** An agent claims a ticket with a 20-minute lease and renews it with a heartbeat while it works. If the agent dies, the lease lapses and the reconciler returns the ticket to **Ready** for another agent.

## Workflows and event flow

Every piece of work is a Temporal workflow with a stable id, so a crash, restart or duplicate event resumes the same workflow instead of starting a second one.

| Workflow | Started by | What it does |
| --- | --- | --- |
| `TicketWorkflow` (one per ticket) | Issue enters **Ready** | Claims the ticket, runs the engineer, opens the PR, waits for review and QA, requests merge approval, releases the claim. After 3 rounds of requested changes it hands the ticket to you. |
| `ReviewWorkflow` | PR opened or updated | Runs the reviewer and posts an approval or requested changes |
| `QAWorkflow` | Reviewer approved and CI green | Runs QA against the PR's preview build and reports results |
| `PlanningWorkflow` | Daily schedule; `needs-breakdown` label | PM grooms the backlog; tech lead breaks epics into ready issues |
| `DeployWorkflow` (Phase 3) | Merge to main | Deploys to staging, runs smoke tests, asks approval, deploys to production |
| `ReconcilerWorkflow` | Every 5 minutes | Compares the board, claims and running workflows; releases expired leases; restarts stuck work; catches missed webhooks |
| `DigestWorkflow` | Daily at 8:00 CT | Posts what shipped, what is blocked, spend, and what needs you |
| `BudgetGuard` | End of every run | Adds the run's cost; pauses an agent at its cap and asks you whether to raise it |

**How an event becomes work**

1. GitHub sends a webhook. The receiver verifies its signature, stores it in `events_inbox` (de-duplicated by delivery id), and replies immediately.
2. A router maps the event to a workflow and starts or signals it by a stable id, such as `ticket-<repo>-<issue>`.
3. Activities do the real work: an agent run in a sandbox, GitHub API calls, chat messages. Each has a timeout and a retry policy; permission errors are not retried.
4. The reconciler polls GitHub as a safety net, so a missed webhook delays work by minutes and never loses it.
5. Approvals post a chat message with Approve and Reject buttons. Your click signals the waiting workflow. Unanswered requests get a reminder after 4 hours and are never auto-approved.

## Agent runtime and sandboxes

Every agent run is a fresh, disposable Docker container holding the repo, the toolchain and the Claude Agent SDK; the worker starts it, collects a structured result, and destroys it.

**One run, start to finish**

1. The `run_agent(role, ticket)` activity creates a container from the product's image (for example `sandbox-node`) under the gVisor runtime, with 2 vCPU and 4 GB by default and no host mounts.
2. It injects two short-lived credentials: a GitHub App token scoped to that one repo, and a per-run token for the model proxy. The real Anthropic key never enters a sandbox.
3. It clones the repo, checks out a work branch, and writes a task file holding the ticket text (marked as untrusted input) and the product brain.
4. It runs the Claude Agent SDK with the role's config: system prompt, allowed tools, MCP servers, hooks, and hard limits on turns and spend per run.
5. The agent commits, pushes its branch and returns a JSON result: summary, tests run, PR body, open questions.
6. The worker records the run in Postgres and Langfuse, then destroys the container. A 90-minute wall-clock timeout kills anything still running.

**Isolation and capacity**

- Network egress from sandboxes goes through an allowlist proxy: package registries, GitHub, and the model proxy only.
- The model proxy is LiteLLM running on the worker server. It holds the Anthropic key, enforces per-run budgets, and logs cost, so it moves into Phase 1.
- One prebuilt image per product stack, with dependencies and the Claude Code CLI installed, rebuilt weekly.
- A worker server with 8 vCPU and 32 GB runs about 4 to 6 sandboxes at once. Add workers to add engineers.

## Guardrails and security

Agents can propose anything but cannot make an irreversible change alone; GitHub, hooks and credentials enforce that, not prompts.

**GitHub**

- Branch protection on `main`: PR required, CI must pass, one approval from a code owner (you), no force pushes.
- One GitHub App per role with least privilege (see the roster). The reviewer cannot push; engineers cannot merge.
- `CODEOWNERS` puts `.github/`, `infra/` and auth code under your review only.
- Only issues created or labeled by you, the PM or the tech lead become `agent-ready`. Issues from outsiders wait for your triage.

**Agent behavior**

- Pre-tool-use hooks block force pushes, deletes outside the workspace, edits to CI, secrets or infra paths, and network calls outside the allowlist.
- Hard caps on turns and spend per run, plus a daily cap per agent.
- Issue text, PR comments, web pages and dependency docs are data, not instructions. Prompts say so, and any request for credentials or policy changes stops the run and alerts you.
- Agents cannot edit their own prompts, configs or budgets.

**Secrets and servers**

- Secrets are SOPS-encrypted in the repo and decrypted only on the servers. No production credentials ever enter a sandbox.
- GitHub tokens expire within an hour; rotate the Anthropic key quarterly.
- SSH keys only, a host firewall, and admin UIs (Temporal, Grafana, Langfuse) reachable only over a private network such as Tailscale or WireGuard.
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
| An agent run crashes | The activity fails | Temporal retries twice with backoff, then labels the ticket `needs-human` |
| An agent loops without progress | Turn or spend cap | The run stops; the ticket gets `needs-human` and a summary |
| A worker server goes down | Heartbeats stop | Leases lapse, tickets return to **Ready**, other workers continue; you get an alert |
| The control server goes down | External uptime check | Workers pause safely; workflows resume from saved state on restart, or from backup |
| Anthropic API errors or rate limits | 429 and 5xx responses | Backoff and retry; after 30 minutes new runs pause and you are alerted; resumes on its own |
| A webhook is missed | Reconciler | Picked up within 5 minutes |
| An agent hits its daily cap | `BudgetGuard` | That agent pauses; you get an approval request to raise the cap |
| Engineer and reviewer disagree 3 times | `TicketWorkflow` | Escalates the ticket to you |
| A merge breaks main | CI on main, Sentry | A revert PR is opened for your approval (DevOps agent in Phase 3) |

In Phases 1 and 2 the control server is a single machine. That is acceptable because all state is persisted and backed up; a standby replica is a Phase 3 item.

## Phased roadmap

Five phases over about 12 weeks; each phase starts only when the previous gate is met, and the last one publishes the platform under MIT.

&#91;embedded content: Roadmap · 5 phases, 4 gates, about 12 weeks\]

Week counts are targets, not commitments; the gates decide when to move on.

**Phase 0: Foundations (week 1)**

- [ ] Pick the first product and a server provider
- [ ] Create the `agent-company` repo; provision 2 VPS with a private network, firewall and SSH keys
- [ ] Bring up the Compose stack: Postgres with pgvector, Valkey, Temporal, LiteLLM, Caddy
- [ ] Create one GitHub App per role; add branch protection and `CODEOWNERS` to the first product
- [ ] Set up SOPS secrets and daily off-site backups, and test a restore
- [ ] Write the first product brain

**Phase 1: Engineer + reviewer (weeks 2 to 4)**

- [ ] Build the `core/` interfaces: model, tickets, sandbox, chat, budget
- [ ] Build the sandbox image for the first product's stack, with gVisor and the egress allowlist
- [ ] Write the engineer and reviewer prompts, configs and hooks
- [ ] Build the webhook receiver, `TicketWorkflow`, `ReviewWorkflow` and `ReconcilerWorkflow`
- [ ] Add the claims, runs and spend tables, per-run and daily caps, and Langfuse tracing
- [ ] Pilot on 10 small tickets and tune prompts from the failures

**Phase 2: Full product loop (weeks 5 to 7)**

- [ ] Add the PM, tech lead and QA agents with `PlanningWorkflow` and `QAWorkflow`
- [ ] Add chat approvals with buttons, `DigestWorkflow` and `BudgetGuard`
- [ ] Give QA preview builds and Playwright
- [ ] Add the memories table and lesson capture
- [ ] Build Grafana dashboards and alerts

**Phase 3: Scale and harden (weeks 8 to 10)**

- [ ] Add the DevOps/SRE agent and `DeployWorkflow`
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
| Temporal | MIT | Separate container | None |
| LiteLLM | MIT core; enterprise folder licensed separately | Separate container | Core features only |
| Langfuse | MIT core; enterprise folder licensed separately | Separate container | Core features only |
| Postgres | PostgreSQL License | Separate container | None |
| Valkey | BSD-3-Clause | Separate container | None |
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
| Prompt injection through issues or dependencies | Untrusted-input rules, egress allowlist, no secrets in sandboxes, human gates |
| Your reviews become the bottleneck | Small PRs with clear summaries, approvals batched in the digest, opt-in auto-merge for low-risk labels later |
| Single control server | Daily off-site backups, monthly restore drill, standby replica in Phase 3 |
| Vendor pricing or terms change | Thin interfaces; LiteLLM already in the path to reroute models |
| Building the platform crowds out product work | Do not start a phase until the previous phase meets its exit criteria |

**Decisions (closed November 2, 2026)**

- [x] **Language: Python.** It has mature Temporal and Claude Agent SDK packages, plus the data tooling the memory and dashboards need.
- [x] **Chat for approvals: Slack** on the free tier, behind the `Chat` adapter so another tool can be swapped in later.
- [x] **Servers: any provider and region you prefer.** The plan only needs two Linux servers with Docker: a control server with about 4 vCPU and 16 GB, and a worker server with about 8 vCPU and 32 GB.
- [x] **First product: any.** The platform is product-agnostic; onboarding a product means writing its product brain and a sandbox image for its stack.
- [x] **Spend ceiling: $250 a day across all agents in Phases 1 and 2** (at most about $7,500 a month), **raised to $500 a day in Phase 3** for the larger engineer pool. Slack alerts at 70%; at 100% agents pause and you can raise the ceiling with one tap. It is a ceiling, not a forecast: use the first two weeks of real spend to tune it.

**Next steps this week**

1. Pick the first product and a server provider; any choice works.
2. Create the `agent-company` repo, two VPS servers and the private network.
3. Write the product brain for the first product. Claude can draft it from the repo for your edits.
4. Scaffold Phase 1: Compose stack, core interfaces, engineer and reviewer agents, `TicketWorkflow` and `ReviewWorkflow`.
