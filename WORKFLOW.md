# Built Diffrent: AI dev team workflow

This repo is a Claude Code workspace that runs three tools as one team:

| Tool | What it is | Owns |
| --- | --- | --- |
| [gstack](https://github.com/garrytan/gstack) | Skills that act as a CEO, eng manager, reviewer, QA lead, CSO and release engineer | Product review, plan review, code review, QA, security, release |
| [oh-my-claudecode (OMC)](https://github.com/Yeachan-Heo/oh-my-claudecode) | Multi-agent orchestration: interviews, consensus planning, autopilot, teams | Requirements, planning, parallel builds of standard features |
| [Superpowers](https://github.com/obra/superpowers) | Disciplined engineering skills: worktrees, plans, strict TDD, verification | Test-first builds of critical features only |

The rules that keep them from competing live in [CLAUDE.md](CLAUDE.md). This document
explains the setup, the `/devteam` pipeline, and maintenance.

## 1. Getting started (each teammate, once)

```bash
scripts/setup-prereqs.sh          # Node.js LTS, git, Bun, tmux (+ C toolchain for gstack /cso)
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
(cd ~/.claude/skills/gstack && ./setup --team --no-skill-routing)
claude                            # in this repo; accept the project plugins when prompted
scripts/smoke-test.sh             # every prerequisite runs, every pipeline command loads
```

### Prerequisites: `scripts/setup-prereqs.sh`

It detects the OS (macOS, Debian/Ubuntu, RHEL family, Fedora, Arch, openSUSE, Alpine,
WSL) and installs only what is missing:

| Prerequisite | How it's installed | Why |
| --- | --- | --- |
| Node.js LTS | official nvm installer, then `nvm install --lts` (no root needed) | OMC, gstack's team-mode hook, the devteam state helper |
| git | system package manager | everything |
| Bun ≥ 1.3.3 | official installer (`curl -fsSL https://bun.sh/install \| bash`) | gstack builds its tools with Bun |
| tmux | system package manager (Homebrew on macOS) | OMC team workers |
| libatomic (Linux) | system package manager | the official Node.js 26+ binaries need it |
| static-capable C compiler (optional) | `gcc glibc-static`, `build-essential`, Xcode CLT, … | gstack `/cso` builds a native helper; without it `/cso` reports "not assessed" |

It ends by printing every version. On native Windows it prints the `winget` commands
instead; use WSL for full support. tcsh/csh users: add `~/.bun/bin` to `PATH` in
`~/.cshrc`, because Bun's installer only edits bash/zsh/fish files.

### How the three tools are installed

Each uses the install method from its own README, at project level wherever the tool
supports it:

| Tool | Install | Project-level part (committed to this repo) |
| --- | --- | --- |
| gstack | clone to `~/.claude/skills/gstack`, `./setup --team --no-skill-routing`, then `gstack-team-init required` | `CLAUDE.md` gstack section, `.claude/hooks/check-gstack.cjs`, the Skill gate hook in `.claude/settings.json`. Every session blocks gstack skills until gstack is installed and auto-updates it (stable channel) |
| OMC | `/plugin marketplace add https://github.com/Yeachan-Heo/oh-my-claudecode` + `/plugin install oh-my-claudecode`, then `/oh-my-claudecode:omc-setup --local` | project-scope plugin: `enabledPlugins` and `extraKnownMarketplaces` in `.claude/settings.json`; `.claude/CLAUDE.md` (OMC's local setup); `.claude/omc.jsonc` |
| Superpowers | `/plugin install superpowers@claude-plugins-official` | project-scope plugin in `.claude/settings.json` |

Because the plugins are enabled in the project's `.claude/settings.json`, they are on by
default for anyone who opens the repo. The gstack hook makes gstack required.

Settings that keep the tools in their lanes:

- **gstack `--no-skill-routing`**: no per-prompt hook suggesting gstack skills for
  work OMC or Superpowers owns.
- **`.claude/omc.jsonc` `keywordDetector.disabled`**: OMC no longer hijacks plain
  English. "tdd" and "test first" would start OMC TDD (Superpowers owns that). "code
  review" and "security review" belong to gstack. "build me an app", "I want a …",
  "ralph" and "ultragoal" would start autonomous runs outside the pipeline.
  `/oh-my-claudecode:*` commands still work.
- **`.claude/omc.jsonc` `autopilot.skipValidation`**: autopilot builds and tests but
  leaves code review and the security pass to gstack's `/review` and `/cso`.
- **`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`** (project `.claude/settings.json`):
  needed by OMC `/team`.
- **CLAUDE.md**: the ownership table, "one driver at a time", and the critical
  criteria. Superpowers' bootstrap defers to CLAUDE.md.

## 2. The `/devteam` pipeline

```bash
scripts/devteam.sh path/to/goal.md            # or: scripts/devteam.sh "a todo app"
scripts/devteam.sh templates/goal-template.md --dry-run   # show the phases, change nothing
scripts/devteam.sh                            # resume the current run
scripts/devteam.sh --status                   # where is the run?
scripts/devteam.sh goal.md -- --model sonnet  # extra arguments go to claude
```

Inside Claude Code, use `/devteam <goal-file or goal text>`. With no argument it
resumes. The launcher and the slash command take the same argument: an existing file
path is read as the goal; anything else is the goal text. Either way the goal is
saved to `.devteam/goal.md`. The launcher prepares the run, then opens Claude Code
with `/devteam <argument>`.

Start from [templates/goal-template.md](templates/goal-template.md): product summary,
users, features, critical features, tech stack, acceptance criteria.

### Phases

| # | Phase | Owner | What happens |
| --- | --- | --- | --- |
| 1 | intake | devteam | goal → `.devteam/goal.md`, run → `.devteam/state.json` |
| 2 | office-hours | gstack | `/office-hours`: demand, users, wedge, scope |
| 3 | plan-ceo-review | gstack | `/plan-ceo-review`: founder-level scope review |
| 4 | deep-interview | OMC | `/oh-my-claudecode:deep-interview`, **only for gaps** left after 2–3; skipped when there are none |
| 5 | ralplan | OMC | `/oh-my-claudecode:ralplan`: planner/architect/critic consensus plan |
| 6 | plan-eng-review | gstack | `/plan-eng-review`: architecture, data flow, edge cases, test plan |
| 7 | classify | devteam | every feature → critical or standard in `.devteam/features.md` |
| 8 | **approve-plan** | **you** | **pause**: approve the plan and the critical list. The only stop; everything below runs unattended |
| 9 | build-critical | Superpowers | per critical feature: own worktree (`.worktrees/<slug>`, branch `devteam/crit-<slug>`), feature plan, red → green → refactor, verified |
| 10 | build-standard | OMC | `/oh-my-claudecode:team` for 2+ independent features, `/oh-my-claudecode:autopilot` otherwise, scoped to standard features only |
| 11 | merge | devteam | critical branches merged into the run branch `devteam/<run-id>`; full suite green; worktrees removed |
| 12 | review | gstack | `/review` |
| 13 | qa | gstack | `/qa` |
| 14 | cso | gstack | `/cso`; high and critical findings fixed (test-first in critical code) |
| 15 | ship | gstack | `/ship`: merge base, tests, version and changelog, push the run branch, open the PR |

`scripts/lib/phases.txt` is the single source for this list. The dry run, the state
file, the slash command and the smoke test all read it.

### Interactive planning, unattended build

Phases 1–8 are interactive: gstack and OMC ask you about the product and the
requirements, and you approve the plan. Approving it approves the rest. Phases 9–15 then
run without questions or approval stops until `/ship` opens the PR. Merging the PR is
yours.

How the unattended half is kept unattended:

| Would stop | Handled by |
| --- | --- |
| Claude Code permission prompts | `scripts/devteam.sh` starts Claude in the `auto` permission mode: routine actions run, risky ones are refused rather than asked. Override with `-- --permission-mode <mode>`. If you start with `/devteam` inside Claude Code, switch to auto mode yourself (Shift+Tab) |
| gstack questions (`/review`, `/qa`, `/ship`) | The hook runs gstack commands with `GSTACK_SESSION_KIND=spawned`, gstack's no-human mode: it takes the recommended option and never a destructive one |
| OMC and Superpowers questions | AskUserQuestion is denied with instructions to take the recommended option; Superpowers' plan hand-off is skipped |
| Claude ending its turn mid-run | A Stop hook sends it back to work while the run is unfinished. If five stops in a row come with no recorded progress, it lets the session stop |

All three are in `.claude/hooks/devteam-autonomy.mjs`. They act only on the session
driving the run, and only after the plan is approved.

Every decision taken in place of a question goes to `.devteam/artifacts/decisions.md`
and is linked from the PR.

The run still stops, as **blocked**, when it needs something only a person can give: a
secret, API key, account or login, a security finding it can't fix, or tests that keep
failing. The reason is in `scripts/devteam.sh --status` and `.devteam/artifacts/<phase>.md`.
Provide what's needed, then run `scripts/devteam.sh` to resume. Usage limits, an expired
login or a closed terminal also end a run; run long jobs inside `tmux` and resume the
same way.

`/ship` needs the GitHub CLI (`gh`), logged in, to open the PR. The launcher warns when
it isn't, and the plan approval lists it, so you can fix it before walking away.

### Critical vs standard

A feature is **critical** if a bug in it could:

- lose money
- expose or corrupt data
- break security or access control
- violate legal or compliance rules
- cause an irreversible action
- break the product's core value

Everything else is **standard**.

- If the goal has a non-empty **Critical features** section, that list is used. Other
  features that meet a criterion are flagged as *suggested critical* for you to decide.
- Otherwise the list is inferred from the criteria.
- Either way it is shown at the approve-plan gate, and nothing is built until you approve.

### One driver at a time

Phases run strictly in sequence; no two tools' workflows overlap. Critical features are
built one after another, each in its own worktree. Standard features are built after
that, on the run branch, and only standard features are built there: OMC is told
explicitly which critical features to leave alone. Parallelism happens inside a phase,
for example OMC `/team` workers, never across tools.

### Progress, gates and resuming

`.devteam/state.json` records the run id, run branch, goal hash, every phase's status
(pending, in_progress, done, skipped, blocked, awaiting_approval, approved), notes,
artifact paths, and every feature's class, status, branch and worktree. Claude updates
it only through `node scripts/lib/devteam.mjs` (`start`, `done`, `skip`, `block`, `gate`,
`approve`, `feature`, …). That helper refuses to start a phase before the earlier ones
finish, so the gate is a real stop.

- **Stopping**: press Esc, or ask Claude to stop (in the unattended half it records the
  phase as blocked, "paused by the user"). `scripts/devteam.sh` or `/devteam` with no
  argument resumes at the first unfinished phase. An interrupted phase continues from
  its artifacts and notes; it is not redone.
- **Same goal again**: resumes. **A different goal** while a run is open is refused;
  pass `--new` to archive the open run to `.devteam/archive/<run-id>/` and start fresh.
- Each phase leaves a summary in `.devteam/artifacts/<phase>.md`; gstack's own
  documents stay under `~/.gstack/projects/` and are registered in the state file.

## 3. Keeping the tools current: `scripts/update-agents.sh`

Runs weekly (Mondays 09:17) and can be run by hand at any time:

1. **Prerequisites** to their latest releases:
   - Node.js to the latest LTS, through whatever manages it (n, nvm, volta, fnm, Homebrew,
     or the system package manager). The new binary is checked to start; if it doesn't,
     the previous version is restored.
   - git and tmux through the package manager (`sudo -n` when unattended)
   - Bun via `bun upgrade`
2. **Snapshot** of the tools: the gstack commit, Claude Code's `installed_plugins.json`,
   the installed plugin versions, and the project's `.claude/` and `CLAUDE.md`. Saved in
   `.devteam/agents/backups/`; the last four are kept.
3. **Update** the tools:
   - gstack: `bin/gstack-upgrade`, its deterministic `/gstack-upgrade`, on the stable
     channel
   - OMC: marketplace update, plugin update, and the project setup again
   - Superpowers: marketplace update, plugin update
4. **Smoke test**: `scripts/smoke-test.sh` checks that every prerequisite runs, gstack's
   install and team hook are healthy, and a fresh headless Claude Code session (Haiku,
   a fraction of a cent) lists every pipeline command and both plugins.
5. **Rollback on failure**: gstack goes back to its previous commit and is rebuilt; the
   plugin versions and project files are restored. gstack's session-start auto-update is
   held (`auto_upgrade false`) so it can't pull the failed release again; the next
   successful run lifts the hold. The script exits 1.
6. Prints before and after versions; exits 0 when everything passes.

Scheduling: the first run installs the weekly job. It uses launchd on macOS, a systemd
user timer on Linux (`Persistent=true` catches up missed runs), or cron when systemd
isn't available. Each checkout gets its own entry.

```bash
scripts/update-agents.sh --install-schedule   # (re)install the weekly job only
scripts/update-agents.sh --remove-schedule
scripts/update-agents.sh --simulate-failure   # rehearse the rollback
systemctl --user list-timers | grep devteam   # Linux: next run
tail .devteam/agents/logs/update-agents.log   # scheduled runs log here
```

On Linux, a systemd user timer runs only while you have a session unless lingering is
on: `loginctl enable-linger $USER` (may need an admin).

Rollback covers the three tools. Prerequisites are guarded differently: a Node.js that
won't start is reverted immediately. Distro packages and Bun are not rolled back, and
the smoke test reports them if they break.

## 4. Files

| Path | Purpose |
| --- | --- |
| `CLAUDE.md` | Tool-ownership rules (plus gstack's required section) |
| `WORKFLOW.md` | This document |
| `templates/goal-template.md` | Goal file template |
| `.claude/commands/devteam.md` | The `/devteam` slash command (orchestrator instructions) |
| `.claude/settings.json` | Project plugins, OMC marketplace, gstack Skill gate, unattended-run hooks, agent-teams env |
| `.claude/CLAUDE.md`, `.claude/skills/wiki/` | Written by OMC's `omc-setup --local` |
| `.claude/omc.jsonc` | OMC project config (keyword triggers, autopilot validation) |
| `.claude/hooks/check-gstack.cjs` | gstack team-mode gate |
| `.claude/hooks/devteam-autonomy.mjs` | Keeps the post-approval half of a run unattended |
| `scripts/setup-prereqs.sh` | Install missing prerequisites, print versions |
| `scripts/devteam.sh` | Launcher / dry run / resume / status |
| `scripts/update-agents.sh` | Weekly update with smoke test and rollback |
| `scripts/smoke-test.sh` | Prerequisites run, every pipeline command loads |
| `scripts/lib/phases.txt` | The pipeline's phases, owners and commands |
| `scripts/lib/devteam.mjs` | State helper (ordering, gates, features, goal parsing) |
| `scripts/lib/common.sh` | Shared shell helpers (OS detection, package managers, versions) |
| `.devteam/` (git-ignored) | `goal.md`, `state.json`, `features.md`, `artifacts/`, `archive/`, `agents/` (update backups and logs) |

## 5. Troubleshooting

- **A command is missing in Claude Code**: run `scripts/smoke-test.sh`; it names the
  tool and command. Restart Claude Code after plugin updates.
- **gstack skills are blocked**: gstack is missing or Node.js isn't on `PATH`; the
  message says which. Install gstack (section 1) or Node.js.
- **`/cso` says "not assessed"**: no static-capable C compiler. Run
  `scripts/setup-prereqs.sh`, then `cd ~/.claude/skills/gstack && ./setup --team --no-skill-routing`.
- **`node: libatomic.so.1: cannot open shared object file`**: run
  `scripts/setup-prereqs.sh`, which installs libatomic.
- **A run is stuck at the gate**: that is by design. Answer the approval question, or run
  `/devteam` to be asked again.
- **A run is blocked**: `scripts/devteam.sh --status` shows the phase and what it needs.
  Provide it, then run `scripts/devteam.sh` to resume.
- **Permission prompts during the unattended half**: the session isn't in auto mode.
  Start runs with `scripts/devteam.sh`, or press Shift+Tab until auto mode is shown.
- **Start over**: `scripts/devteam.sh --new <goal>` archives the current run.
