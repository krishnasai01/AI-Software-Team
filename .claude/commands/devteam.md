---
description: Run the AI dev team pipeline (gstack → OMC → Superpowers/OMC builds → gstack review/QA/CSO → ship) for a goal file or goal text
argument-hint: <goal-file | goal text>  (empty = resume)
allowed-tools: Bash(scripts/devteam.sh:*), Bash(node scripts/lib/devteam.mjs:*), Bash(git status:*), Bash(git switch:*), Bash(git branch:*), Bash(git worktree:*), Bash(git merge:*), Bash(git log:*), Bash(git diff:*)
---

# /devteam

You are the orchestrator for this repo's AI dev team. Follow the tool-ownership
rules in CLAUDE.md exactly: each phase has one owning tool, and only that tool
drives execution while the phase runs.

Argument: `$ARGUMENTS`

## 1. Intake (every invocation)

Run, passing the argument as ONE shell-quoted string (omit it when empty):

```bash
scripts/devteam.sh --prepare '<the argument>'
```

The script decides file vs text: an existing file path is read, anything else
is goal text. Either way the goal ends up in `.devteam/goal.md` and the run in
`.devteam/state.json`. It prints the run status. If it reports a different
goal already in progress, stop and ask the user whether to resume that run
(`/devteam` with no argument) or archive it (`scripts/devteam.sh --new '<arg>' --prepare`).

Then read `.devteam/goal.md`, `.devteam/state.json` and, if present,
`.devteam/features.md`.

## 2. Resume rules

- Start at `current_phase` in state.json. Never redo a phase marked done,
  skipped or approved.
- A phase marked `in_progress` was interrupted: check its `artifacts` and
  `notes`, then the git branch, worktrees (`git worktree list`) and per-feature
  status, and continue where it stopped rather than starting over.
- A gate marked `awaiting_approval` means: ask again (section 4).

## 3. Recording progress

Use only the state helper; never edit state.json by hand. It enforces phase
order and refuses to move past an unapproved gate. If it refuses, do not work
around it.

```bash
node scripts/lib/devteam.mjs start <phase>
node scripts/lib/devteam.mjs artifact <phase> <path>        # every file a phase produces
node scripts/lib/devteam.mjs note <phase> "<one line>"
node scripts/lib/devteam.mjs done <phase> "<one-line outcome>"
node scripts/lib/devteam.mjs skip <phase> "<reason>"
node scripts/lib/devteam.mjs gate <phase>                   # before asking for approval
node scripts/lib/devteam.mjs approve <phase>                # only after the user says yes
node scripts/lib/devteam.mjs feature "<name>" <critical|standard> <planned|in_progress|built|merged> [--branch b] [--worktree w]
node scripts/lib/devteam.mjs status
```

After each phase, also save a short summary of its outcome (decisions, open
questions, where its full output lives) to `.devteam/artifacts/<phase>.md` and
register it with `artifact`. Later phases, a resumed run, and work inside a
worktree depend on these summaries. gstack writes its own documents under
`~/.gstack/projects/`; register those paths too.

## 4. Approval gates

At `approve-plan` and `approve-ship`: run `gate <phase>`, present what is being
approved, then ask with AskUserQuestion (Approve / Request changes / Stop
here). Record `approve <phase>` ONLY on an explicit approval. On "Request
changes", make the changes with the phase's owning tool, then ask again. On
"Stop here", leave the gate `awaiting_approval` and end; `/devteam` resumes at
the gate. Never approve on the user's behalf, and never treat silence or an
earlier approval as approval of a later gate.

## 5. Phases, in order

Invoke each tool's skill with the Skill tool, using the exact names below.
Give every skill the goal (`.devteam/goal.md`) and the artifacts of the earlier
phases as context.

1. **intake** (devteam): done by `scripts/devteam.sh --prepare`.
2. **office-hours** (gstack): `/office-hours`, with the goal as the idea. Answer
   from the goal where it already says; ask the user for the rest.
3. **plan-ceo-review** (gstack): `/plan-ceo-review` on the office-hours design doc.
4. **deep-interview** (OMC): list the requirements that are still ambiguous after
   steps 2 and 3. If none, `skip deep-interview "no gaps after gstack reviews"`.
   Otherwise run `/oh-my-claudecode:deep-interview` scoped to those gaps only;
   do not re-ask what is already settled.
5. **ralplan** (OMC): `/oh-my-claudecode:ralplan` with the goal, the gstack
   design doc and CEO review, and any deep-interview spec. Register the
   resulting `.omc/plans/` file.
6. **plan-eng-review** (gstack): `/plan-eng-review` on the ralplan plan. Fold its
   required changes back into the plan file.
7. **classify** (devteam): write `.devteam/features.md`, one row per feature in
   the plan, and register every feature with `feature`:

   ```markdown
   | # | Feature | Class | Why (criterion or "none") | Depends on |
   ```

   - If the goal has a non-empty "Critical features" section, those are
     critical. Mark any other feature that meets a criterion below as
     **suggested critical** and say why; the user decides at the gate.
   - Otherwise infer: a feature is **critical** if a bug in it could lose
     money, expose or corrupt data, break security or access control, violate
     legal or compliance rules, cause an irreversible action, or break the
     product's core value. Everything else is **standard**.
8. **approve-plan** (you): show the plan summary and the full critical/standard
   list (state whether critical came from the goal or was inferred). Gate per
   section 4. Apply any reclassification to features.md and state before
   approving.
9. **build-critical** (Superpowers): first create the run branch from the
   default branch if it doesn't exist (`git switch -c <branch from state.json>`).
   Then for each critical feature, one at a time, in dependency order:
   - `superpowers:using-git-worktrees`: worktree `.worktrees/<feature-slug>` on
     branch `devteam/crit-<feature-slug>`, based on the run branch.
   - `superpowers:writing-plans` for that feature only, from the approved plan.
   - `superpowers:subagent-driven-development` executing that plan with
     `superpowers:test-driven-development`: a failing test before every piece
     of production code, red → green → refactor.
   - `superpowers:verification-before-completion`: the feature's tests and the
     full suite pass in the worktree, with the output shown.
   - Commit on the feature branch. Do not merge (merging is step 11); if
     `finishing-a-development-branch` comes up, keep the branch as is.
   - `feature "<name>" critical built --branch … --worktree …`.
   If a critical feature depends on a standard one, note it in features.md;
   build that standard dependency first in step 10's way, on the run branch,
   then continue here.
10. **build-standard** (OMC): on the run branch in the main checkout. Pass OMC
    an explicit task listing ONLY the standard features from features.md, the
    plan file, and a line saying "Do not implement these critical features;
    they are built on separate branches: …". Use
    `/oh-my-claudecode:team` when there are two or more independent standard
    features, `/oh-my-claudecode:autopilot` otherwise. Mark each feature
    `built`. Skip the phase when there are no standard features.
11. **merge** (devteam): merge each `devteam/crit-*` branch into the run branch
    (`git merge --no-ff`), resolve conflicts, run the full test suite until
    green, mark features `merged`, then remove the worktrees
    (`git worktree remove`).
12. **review** (gstack): `/review` on the run branch's diff against the default branch.
13. **qa** (gstack): `/qa`.
14. **cso** (gstack): `/cso`. Fix high and critical findings (critical features
    are fixed test-first) before continuing.
15. **approve-ship** (you): summarize review, QA and CSO results and anything
    left open. Gate per section 4.
16. **ship** (gstack): `/ship`.

When `ship` is done, show `node scripts/lib/devteam.mjs status` and the PR link.

## 6. Rules

- One driver at a time: never run two phases at once, and never let one tool's
  skill do another phase's job (no Superpowers TDD for standard features, no
  OMC review/QA, no gstack builds).
- A phase is done when its output exists and is registered, not when a skill
  says so. Unfinished work stays `in_progress`.
- When the user asks to stop, leave state as it is; `/devteam` resumes it.
