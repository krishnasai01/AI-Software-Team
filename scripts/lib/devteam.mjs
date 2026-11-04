#!/usr/bin/env node
// Phase list, goal parsing and .devteam/state.json for the /devteam pipeline.
// Called by scripts/devteam.sh and by Claude while running /devteam, so every
// state change goes through one place and stays ordered and gated.
//
//   devteam.mjs phases [--json]
//   devteam.mjs critical <goal.md>               list the goal's "Critical features"
//   devteam.mjs init <goal.md> <source>          create state, or resume the same goal
//   devteam.mjs status | next | mode
//   devteam.mjs start|done <phase> [note]
//   devteam.mjs skip <phase> <reason>
//   devteam.mjs block <phase> <reason>           stop an unattended run that needs a human
//   devteam.mjs note <phase> <text>
//   devteam.mjs artifact <phase> <path>
//   devteam.mjs gate <phase>                     mark an approval gate as waiting
//   devteam.mjs approve <phase>                  record the user's approval
//   devteam.mjs feature <name> <critical|standard> [status] [--branch b] [--worktree w]
//   devteam.mjs archive                          move the current run aside
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '../..');
const stateDir = path.join(repoRoot, '.devteam');
const statePath = path.join(stateDir, 'state.json');

const FINISHED = new Set(['done', 'skipped', 'approved']);

function fail(msg, code = 1) {
  process.stderr.write(`devteam: ${msg}\n`);
  process.exit(code);
}

export function loadPhases() {
  const text = fs.readFileSync(path.join(here, 'phases.txt'), 'utf8');
  return text.split('\n')
    .filter((l) => l.trim() && !l.trimStart().startsWith('#'))
    .map((l) => {
      const [id, owner, commands, kind, summary] = l.split('|').map((s) => s.trim());
      return { id, owner, commands: commands === '-' ? [] : commands.split(/\s+/), kind, summary };
    });
}

// Items under a "Critical features" heading, ignoring HTML comments and
// template placeholders such as "- <feature> — <why>".
export function criticalFeatures(markdown) {
  const lines = markdown.replace(/<!--[\s\S]*?-->/g, '').split('\n');
  const start = lines.findIndex((l) => /^#{1,6}\s*critical features\b/i.test(l));
  if (start < 0) return null;
  const level = lines[start].match(/^#+/)[0].length;
  const items = [];
  for (const line of lines.slice(start + 1)) {
    const h = line.match(/^(#{1,6})\s/);
    if (h && h[1].length <= level) break;
    const m = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*\S)/);
    if (m && !/^<[^>]*>/.test(m[1]) && !/^\(?none\)?\.?$/i.test(m[1])) items.push(m[1]);
  }
  return items;
}

const now = () => new Date().toISOString();
const sha256 = (s) => createHash('sha256').update(s).digest('hex');

function readState() {
  if (!fs.existsSync(statePath)) fail('no run in progress (.devteam/state.json not found)', 2);
  return JSON.parse(fs.readFileSync(statePath, 'utf8'));
}

// The run as the hooks see it: null when there is no readable run.
export function loadState() {
  try { return JSON.parse(fs.readFileSync(statePath, 'utf8')); } catch { return null; }
}

// planning: a human answers questions and approves the plan.
// unattended: every gate is approved; nothing may wait on a human until the run
// is complete or blocked.
export function runMode(state) {
  if (!state) return 'none';
  if (state.status === 'complete' || state.status === 'blocked') return state.status;
  const gates = state.phases.filter((p) => p.kind === 'gate');
  return gates.every((p) => p.status === 'approved') ? 'unattended' : 'planning';
}

function writeState(state) {
  state.updated_at = now();
  const next = state.phases.find((p) => !FINISHED.has(p.status));
  state.current_phase = next ? next.id : null;
  state.status = !next ? 'complete' : ['awaiting_approval', 'blocked'].includes(next.status) ? next.status : 'running';
  fs.mkdirSync(stateDir, { recursive: true });
  const tmp = `${statePath}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`);
  fs.renameSync(tmp, statePath);
}

function phaseOf(state, id) {
  const i = state.phases.findIndex((p) => p.id === id);
  if (i < 0) fail(`unknown phase "${id}" (phases: ${state.phases.map((p) => p.id).join(', ')})`);
  return [state.phases[i], i];
}

// A phase may start only when everything before it is finished, which is what
// makes the approval gates real pauses.
function requireOrder(state, i) {
  const blocker = state.phases.slice(0, i).find((p) => !FINISHED.has(p.status));
  if (blocker) {
    const why = blocker.kind === 'gate' ? 'is waiting for your approval' : `is ${blocker.status}`;
    fail(`cannot advance to "${state.phases[i].id}": "${blocker.id}" ${why}`, 3);
  }
}

function init(goalFile, source) {
  const goal = fs.readFileSync(goalFile, 'utf8');
  const hash = sha256(goal);
  if (fs.existsSync(statePath)) {
    const state = readState();
    if (state.goal.sha256 !== hash) {
      fail('a run for a different goal is in progress; finish it, or pass --new to archive it and start over', 4);
    }
    console.log(`resume ${state.current_phase ?? 'complete'}`);
    return;
  }
  const crit = criticalFeatures(goal);
  const runId = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*/, '').replace('T', '-');
  const state = {
    version: 1,
    run_id: runId,
    branch: `devteam/${runId}`,
    created_at: now(),
    goal: { source, path: path.relative(repoRoot, fs.realpathSync(goalFile)), sha256: hash },
    critical_source: crit && crit.length ? 'goal' : 'inferred',
    critical_from_goal: crit ?? [],
    phases: loadPhases().map((p) => ({ id: p.id, owner: p.owner, kind: p.kind, status: 'pending', notes: [], artifacts: [] })),
    features: [],
  };
  Object.assign(state.phases[0], { status: 'done', started_at: now(), finished_at: now() });
  writeState(state);
  console.log(`new ${state.current_phase}`);
}

function status() {
  const s = readState();
  const mark = { done: 'x', skipped: '-', approved: 'x', in_progress: '>', awaiting_approval: '?', blocked: '!', pending: ' ' };
  console.log(`run ${s.run_id}  branch ${s.branch}  status ${s.status}  mode ${runMode(s)}`);
  console.log(`goal ${s.goal.path} (${s.goal.source}); critical features ${s.critical_source === 'goal' ? 'from the goal' : 'inferred'}`);
  for (const p of s.phases) {
    const extra = p.notes.length ? `  — ${p.notes[p.notes.length - 1]}` : '';
    console.log(`  [${mark[p.status] ?? ' '}] ${p.id.padEnd(16)} ${p.owner.padEnd(12)} ${p.status}${extra}`);
  }
  if (s.features.length) {
    console.log('features:');
    for (const f of s.features) {
      const where = f.worktree ? `  ${f.worktree}` : '';
      console.log(`  ${f.class.padEnd(9)} ${f.status.padEnd(12)} ${f.name}${where}`);
    }
  }
}

function setPhase(cmd, id, text) {
  const s = readState();
  const [p, i] = phaseOf(s, id);
  switch (cmd) {
    case 'start':
      if (p.kind === 'gate') fail(`"${id}" is an approval gate; use "gate ${id}"`);
      requireOrder(s, i);
      if (!FINISHED.has(p.status)) Object.assign(p, { status: 'in_progress', started_at: p.started_at ?? now() });
      break;
    case 'done':
      if (p.kind === 'gate') fail(`"${id}" is an approval gate; only "approve ${id}" finishes it`);
      requireOrder(s, i);
      Object.assign(p, { status: 'done', finished_at: now() });
      break;
    case 'skip':
      if (p.kind === 'gate') fail('approval gates cannot be skipped');
      if (!text) fail('skip needs a reason');
      requireOrder(s, i);
      Object.assign(p, { status: 'skipped', finished_at: now() });
      break;
    case 'block':
      if (p.kind === 'gate') fail(`"${id}" is an approval gate; it already waits for the user`);
      if (!text) fail('block needs a reason');
      requireOrder(s, i);
      Object.assign(p, { status: 'blocked', blocked_at: now() });
      text = `BLOCKED: ${text}`;
      break;
    case 'gate':
      if (p.kind !== 'gate') fail(`"${id}" is not an approval gate`);
      requireOrder(s, i);
      if (p.status !== 'approved') Object.assign(p, { status: 'awaiting_approval', started_at: p.started_at ?? now() });
      break;
    case 'approve':
      if (p.kind !== 'gate') fail(`"${id}" is not an approval gate`);
      requireOrder(s, i);
      Object.assign(p, { status: 'approved', finished_at: now() });
      break;
    case 'note':
      if (!text) fail('note needs text');
      break;
    case 'artifact':
      if (!text) fail('artifact needs a path');
      if (!p.artifacts.includes(text)) p.artifacts.push(text);
      text = undefined;
      break;
  }
  if (text) p.notes.push(text);
  writeState(s);
  console.log(`${id}: ${p.status}; next ${s.current_phase ?? 'none (run complete)'}`);
}

function feature(args) {
  const opts = {};
  const pos = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--branch' || args[i] === '--worktree') opts[args[i].slice(2)] = args[++i];
    else pos.push(args[i]);
  }
  const [name, cls, st] = pos;
  if (!name || !['critical', 'standard'].includes(cls)) fail('usage: feature <name> <critical|standard> [status] [--branch b] [--worktree w]');
  const s = readState();
  let f = s.features.find((x) => x.name === name);
  if (!f) s.features.push((f = { name, class: cls, status: 'planned' }));
  Object.assign(f, { class: cls }, st ? { status: st } : {}, opts);
  writeState(s);
  console.log(`${f.class} ${f.status} ${f.name}`);
}

function archive() {
  if (!fs.existsSync(statePath)) return;
  const s = readState();
  const dest = path.join(stateDir, 'archive', s.run_id);
  fs.mkdirSync(dest, { recursive: true });
  for (const f of ['state.json', 'goal.md', 'features.md']) {
    const src = path.join(stateDir, f);
    if (fs.existsSync(src)) fs.renameSync(src, path.join(dest, f));
  }
  console.log(`archived run ${s.run_id} to ${path.relative(repoRoot, dest)}`);
}

function isMain() {
  try { return fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1]); } catch { return false; }
}

if (isMain()) {
  const [cmd, ...args] = process.argv.slice(2);
  switch (cmd) {
    case 'phases':
      if (args[0] === '--json') console.log(JSON.stringify(loadPhases(), null, 2));
      else for (const p of loadPhases()) console.log([p.id, p.owner, p.commands.join(' ') || '-', p.kind, p.summary].join('\t'));
      break;
    case 'critical': {
      const items = criticalFeatures(fs.readFileSync(args[0], 'utf8'));
      for (const it of items ?? []) console.log(it);
      break;
    }
    case 'init': init(args[0], args[1]); break;
    case 'status': status(); break;
    case 'next': console.log(readState().current_phase ?? 'complete'); break;
    case 'mode': console.log(runMode(loadState())); break;
    case 'start': case 'done': case 'skip': case 'block': case 'note': case 'artifact': case 'gate': case 'approve':
      if (!args[0]) fail(`usage: ${cmd} <phase>`);
      setPhase(cmd, args[0], args.slice(1).join(' ') || undefined);
      break;
    case 'feature': feature(args); break;
    case 'archive': archive(); break;
    default:
      fail('usage: devteam.mjs phases|critical|init|status|next|mode|start|done|skip|block|note|artifact|gate|approve|feature|archive');
  }
}
