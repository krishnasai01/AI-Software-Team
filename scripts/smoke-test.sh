#!/usr/bin/env bash
# Checks that every prerequisite runs and that every command the /devteam
# pipeline uses (scripts/lib/phases.txt, plus /devteam itself) is loaded in a
# fresh Claude Code session. Prints versions; exits non-zero on any failure.
#
# The fresh-session check starts one headless `claude -p` run on Haiku and
# reads the command list from its init event (cost: a fraction of a cent).
#
# Usage: scripts/smoke-test.sh [--no-session]
set -uo pipefail
. "$(dirname "$0")/lib/common.sh"
add_user_tool_paths
detect_os

no_session=0
[ "${1:-}" = --no-session ] && no_session=1
failures=0
pass() { ok "$*"; }
flunk() { warn "FAIL $*"; failures=$((failures + 1)); }

with_timeout() {
  local secs=$1; shift
  if have timeout; then timeout "$secs" "$@"
  elif have gtimeout; then gtimeout "$secs" "$@"
  else "$@"; fi
}

log "prerequisites run"
node -e 'process.exit(0)' 2>/dev/null && pass "node $(node --version)" || flunk "node does not run"
npm --version >/dev/null 2>&1 && pass "npm $(npm --version)" || flunk "npm does not run"
git --version >/dev/null 2>&1 && pass "$(git --version)" || flunk "git does not run"
if [ "$(bun -e 'console.log(6*7)' 2>/dev/null)" = 42 ]; then
  version_ge "$(bun_version)" "$BUN_MIN_VERSION" && pass "bun $(bun_version)" \
    || flunk "bun $(bun_version) is older than gstack's minimum $BUN_MIN_VERSION"
else flunk "bun does not run"; fi
tmux -V >/dev/null 2>&1 && pass "$(tmux -V)" || flunk "tmux does not run"
have claude && claude --version >/dev/null 2>&1 && pass "claude $(claude --version | awk '{print $1}')" || flunk "claude does not run"

log "tools installed"
GSTACK_DIR="$HOME/.claude/skills/gstack"
if [ -f "$GSTACK_DIR/VERSION" ] && [ -x "$GSTACK_DIR/setup" ]; then
  pass "gstack $(cat "$GSTACK_DIR/VERSION") ($(git -C "$GSTACK_DIR" rev-parse --short HEAD 2>/dev/null))"
  "$GSTACK_DIR/setup" --status >/dev/null 2>&1 && pass "gstack setup --status clean" || flunk "gstack setup --status reports broken skill links"
else
  flunk "gstack is not installed at $GSTACK_DIR"
fi
[ -f "$REPO_ROOT/.claude/hooks/check-gstack.cjs" ] && pass "gstack team-mode hook present" || flunk "gstack team-mode hook missing (.claude/hooks/check-gstack.cjs)"

if [ $no_session = 1 ]; then
  log "skipping the fresh-session command check (--no-session)"
else
  log "fresh Claude Code session loads every pipeline command"
  out="$(mktemp)"
  ( cd "$REPO_ROOT" && with_timeout 240 claude -p "Reply with OK" --model haiku \
      --output-format stream-json --verbose --max-turns 1 < /dev/null > "$out" 2>/dev/null )
  node - "$out" "$REPO_ROOT/scripts/lib/devteam.mjs" <<'EOF' || failures=$((failures + 1))
const fs = require('fs');
const [out, dt] = process.argv.slice(2);
const events = fs.readFileSync(out, 'utf8').split('\n').filter(Boolean)
  .map((l) => { try { return JSON.parse(l); } catch { return {}; } });
const init = events.find((e) => e.type === 'system' && e.subtype === 'init');
if (!init) { console.error('warn FAIL no init event from claude -p (not signed in, or it timed out)'); process.exit(1); }
const loaded = new Set(init.slash_commands || []);
import(dt).then(({ loadPhases }) => {
  const want = [['devteam', 'devteam']];
  for (const p of loadPhases()) for (const c of p.commands) want.push([p.owner, c.replace(/^\//, '')]);
  const missing = want.filter(([, c]) => !loaded.has(c));
  const byTool = {};
  for (const [tool, c] of want) (byTool[tool] ??= []).push(c);
  for (const [tool, cmds] of Object.entries(byTool)) {
    const miss = cmds.filter((c) => !loaded.has(c));
    console.log(`${miss.length ? 'warn FAIL' : ' ok'} ${tool}: ${cmds.length - miss.length}/${cmds.length} commands loaded${miss.length ? ` (missing: ${miss.join(', ')})` : ''}`);
  }
  for (const p of init.plugins || []) {
    if (['oh-my-claudecode', 'superpowers'].includes(p.name)) console.log(` ok plugin ${p.name} ${p.version}`);
  }
  for (const name of ['oh-my-claudecode', 'superpowers']) {
    if (!(init.plugins || []).some((p) => p.name === name)) { console.log(`warn FAIL plugin ${name} not loaded`); process.exitCode = 1; }
  }
  console.log(` ok ${loaded.size} commands loaded in total (claude ${init.claude_code_version})`);
  if (missing.length) process.exitCode = 1;
});
EOF
  rm -f "$out"
fi

print_prereq_versions >/dev/null
if [ $failures -gt 0 ]; then
  printf '\n%sSmoke test FAILED%s (%d check(s))\n' "$C_RED" "$C_OFF" "$failures"
  exit 1
fi
printf '\n%sSmoke test passed%s\n' "$C_GREEN" "$C_OFF"
