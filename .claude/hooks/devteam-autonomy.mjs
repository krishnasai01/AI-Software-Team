#!/usr/bin/env node
// Keeps a /devteam run unattended once the plan is approved. Does nothing while
// planning, after the run completes or blocks, or when no run exists.
//
//   devteam-autonomy.mjs bash   PreToolUse Bash: run gstack commands in gstack's
//                               no-human mode (GSTACK_SESSION_KIND=spawned), and
//                               remember which session drives the run
//   devteam-autonomy.mjs ask    PreToolUse AskUserQuestion: deny, with how to decide
//   devteam-autonomy.mjs stop   Stop: keep the driving session going until the run
//                               is complete or blocked
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const root = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const dtDir = path.join(root, '.devteam');
const driverPath = path.join(dtDir, 'driver.json');
const guardPath = path.join(dtDir, 'stop-guard.json');
// Stops allowed through when the run makes no recorded progress between them,
// so a session that cannot move on is not held forever.
const MAX_IDLE_STOPS = 5;

const readJson = (p) => { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; } };
const writeJson = (p, v) => { fs.mkdirSync(dtDir, { recursive: true }); fs.writeFileSync(p, `${JSON.stringify(v)}\n`); };
const reply = (v) => process.stdout.write(JSON.stringify(v));

async function main() {
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  const input = JSON.parse(raw || '{}');

  const { loadState, runMode } = await import(pathToFileURL(path.join(root, 'scripts/lib/devteam.mjs')).href);
  const state = loadState();
  const event = process.argv[2];
  const cmd = input.tool_input?.command ?? '';
  // Only the session running /devteam records state, which identifies it.
  if (state && event === 'bash' && /scripts\/lib\/devteam\.mjs|scripts\/devteam\.sh/.test(cmd)) {
    writeJson(driverPath, { session_id: input.session_id });
  }
  if (runMode(state) !== 'unattended') return;
  const phase = state.current_phase;

  switch (event) {
    case 'bash': {
      if (/gstack/.test(cmd) && !/GSTACK_SESSION_KIND=/.test(cmd)) {
        reply({ hookSpecificOutput: { hookEventName: 'PreToolUse', updatedInput: { ...input.tool_input, command: `export GSTACK_SESSION_KIND=spawned; ${cmd}` } } });
      }
      break;
    }
    case 'ask':
      reply({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason:
            `The plan is approved, so the rest of this /devteam run is unattended and nobody will answer. ` +
            `Take the recommended option (the safe, non-destructive one if none is recommended), append the question, ` +
            `the choice and why to .devteam/artifacts/decisions.md, and carry on. Only if the work truly cannot continue ` +
            `without a person, run: node scripts/lib/devteam.mjs block ${phase} "<what is needed>" and stop.`,
        },
      });
      break;
    case 'stop': {
      const driver = readJson(driverPath);
      if (!driver || driver.session_id !== input.session_id) return;
      const guard = readJson(guardPath);
      const idle = guard && guard.stamp === state.updated_at ? guard.count + 1 : 1;
      writeJson(guardPath, { stamp: state.updated_at, count: idle });
      if (idle > MAX_IDLE_STOPS) return;
      reply({
        decision: 'block',
        reason:
          `The /devteam run is unattended and not finished (current phase: ${phase}). Don't wait for the user: ` +
          `continue it per .claude/commands/devteam.md and record progress with scripts/lib/devteam.mjs. ` +
          `If it truly cannot continue without a person, run: node scripts/lib/devteam.mjs block ${phase} "<what is needed>", then stop.`,
      });
      break;
    }
  }
}

main().catch((e) => { process.stderr.write(`devteam-autonomy: ${e.message}\n`); });
