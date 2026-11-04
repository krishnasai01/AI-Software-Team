# AI dev team workspace

Three tools are installed and enabled for this project: **gstack**, **oh-my-claudecode
(OMC)** and **Superpowers**. Each owns a different part of the work. These rules take
precedence over each tool's own defaults, including OMC's `.claude/CLAUDE.md`, OMC
keyword triggers, and Superpowers' "use a skill for everything" bootstrap. How it all
fits together: WORKFLOW.md.

## Tool ownership

| Work | Owner | Use |
| --- | --- | --- |
| Product review (is this worth building, for whom, what scope) | gstack | `/office-hours`, `/plan-ceo-review` |
| Requirements (filling gaps only) | OMC | `/oh-my-claudecode:deep-interview` |
| Implementation planning | OMC | `/oh-my-claudecode:ralplan` |
| Plan review (architecture, edge cases, test plan) | gstack | `/plan-eng-review` |
| Building **standard** features, including in parallel | OMC | `/oh-my-claudecode:autopilot`, `/oh-my-claudecode:team` |
| Building **critical** features, test-first, one git worktree each | Superpowers | `superpowers:using-git-worktrees`, `writing-plans`, `test-driven-development`, `subagent-driven-development`, `verification-before-completion` |
| Code review | gstack | `/review` |
| QA | gstack | `/qa` |
| Security | gstack | `/cso` |
| Release | gstack | `/ship` |

Rules:

1. **One driver at a time.** Only one tool drives execution at any moment. Finish or
   pause the current tool's phase before invoking another tool's skill. Never run two
   tools' workflows in parallel, and never start a second autonomous mode (autopilot,
   ralph, team, subagent-driven-development) while one is active.
2. **Stay in lane.** Don't use a tool for work it doesn't own, even if it offers to:
   - no Superpowers skills for standard features, planning or review
   - no OMC `review`, `verify`, `release`, `tdd` or security modes (gstack and
     Superpowers own those)
   - no gstack building outside `/review` and `/qa` fixes
   - Superpowers' `brainstorming` is not used: gstack and OMC own product and planning
3. **Critical vs standard.** A feature is **critical** if a bug in it could lose money,
   expose or corrupt data, break security or access control, violate legal or compliance
   rules, cause an irreversible action, or break the product's core value. Everything
   else is **standard**. Critical features are built test-first with Superpowers, each
   in its own git worktree; standard features are built with OMC. A fix to a critical
   feature, at any stage, is also made test-first.
4. **Full pipeline = `/devteam`.** For a new product or a multi-feature goal, use
   `/devteam <goal-file | goal text>` (or `scripts/devteam.sh`). It runs the phases in
   order, records progress in `.devteam/state.json` and stops for approval after
   planning and before `/ship`. Don't skip an approval gate or mark one approved
   without the user's explicit yes.
5. **Small one-off tasks** (a bug fix, a question) don't need the pipeline. Pick the
   owner by the table: fixes in critical code go through Superpowers TDD, other fixes
   are made directly, then gstack `/review`.

## Maintenance

- `scripts/setup-prereqs.sh`: install the missing prerequisites (Node.js LTS, git, Bun, tmux)
- `scripts/update-agents.sh`: update the prerequisites and the three tools, smoke-test them, roll back on failure (runs weekly)
- `scripts/smoke-test.sh`: check that the prerequisites run and every pipeline command loads in a fresh session

## gstack browsing

Use gstack's `/browse` skill for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

## gstack (REQUIRED — global install)

**Before doing ANY work, verify gstack is installed:**

```bash
_GS=""
for _D in "${GSTACK_ROOT:-}" "$HOME/.claude/skills/gstack" "$HOME/.codex/skills/gstack" "$HOME/.factory/skills/gstack" "$HOME/.kiro/skills/gstack" "$HOME/.config/opencode/skills/gstack" "$HOME/.slate/skills/gstack" "$HOME/.cursor/skills/gstack" "$HOME/.openclaw/skills/gstack" "$HOME/.hermes/skills/gstack" "$HOME/.gbrain/skills/gstack" "$HOME/.copilot/skills/gstack" "$HOME/.gemini/antigravity-cli/skills/gstack" "$HOME/.gemini/skills/gstack" "$HOME/.gstack/repos/gstack"; do
  [ -z "$_GS" ] && [ -n "$_D" ] && [ -d "$_D/bin" ] && _GS="$_D"
done
[ -n "$_GS" ] && echo "GSTACK_OK: $_GS" || echo "GSTACK_MISSING"
```

If GSTACK_MISSING: STOP. Do not proceed. Tell the user:

> gstack is required for all AI-assisted work in this repo.
> Install it:
> ```bash
> git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
> cd ~/.claude/skills/gstack && ./setup --team
> ```
> Then restart your AI coding tool.

Do not skip skills, ignore gstack errors, or work around missing gstack.

Using gstack skills: After install, skills like /qa, /ship, /review, /investigate,
and /browse are available. Use /browse for all web browsing (Aside first, the bundled gstack browser as fallback).
Use the resolved install path above for gstack file paths
(default: ~/.claude/skills/gstack).

The Skill gate in .claude/settings.json runs .claude/hooks/check-gstack.cjs with
Node.js and blocks when it cannot run. If a skill is blocked with "node: not
found", tell the user to install Node.js (https://nodejs.org); if it is blocked
with "Cannot find module ... check-gstack.cjs", the hook file is missing from
this checkout. Do not work around either block.
