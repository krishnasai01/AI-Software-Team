#!/usr/bin/env bash
# Weekly updater: moves the prerequisites (Node.js LTS, git, Bun, tmux) and the
# three tools (gstack, oh-my-claudecode, Superpowers) to their latest releases,
# runs scripts/smoke-test.sh, and rolls the tools back if it fails. Each run
# also makes sure the weekly schedule exists (launchd, systemd user timer or cron).
#
# Usage: scripts/update-agents.sh [options]
#   --skip-prereqs       update only the three tools
#   --no-schedule        don't install/refresh the weekly schedule
#   --install-schedule   install/refresh the weekly schedule and exit
#   --remove-schedule    remove the weekly schedule and exit
#   --simulate-failure   treat the smoke test as failed (exercises rollback)
#   --cron               non-interactive run (what the scheduled run uses)
#
# Backups and logs: .devteam/agents/{backups,logs}/. Exit 0 = updated and
# verified; 1 = smoke test failed and the tools were rolled back.
set -uo pipefail
. "$(dirname "$0")/lib/common.sh"
add_user_tool_paths
detect_os

skip_prereqs=0 schedule=1 simulate_failure=0
for arg in "$@"; do
  case "$arg" in
    --skip-prereqs) skip_prereqs=1 ;;
    --no-schedule) schedule=0 ;;
    --simulate-failure) simulate_failure=1 ;;
    --cron) export NONINTERACTIVE=1 ;;
    --remove-schedule) schedule=remove ;;
    --install-schedule) schedule=only ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done
[ -t 0 ] || export NONINTERACTIVE=1

GSTACK_DIR="$HOME/.claude/skills/gstack"
OMC_PLUGIN="oh-my-claudecode@omc"
SP_PLUGIN="superpowers@claude-plugins-official"
PLUGINS_JSON="$HOME/.claude/plugins/installed_plugins.json"
PLUGIN_CACHE="$HOME/.claude/plugins/cache"
AGENTS_DIR="$REPO_ROOT/.devteam/agents"
GSTACK_HOLD="$AGENTS_DIR/gstack-held"
CRON_TAG="# devteam:update-agents $REPO_ROOT"
# One schedule per checkout: unit/agent names carry a hash of the repo path.
SCHED_ID="devteam-update-agents-$(printf '%s' "$REPO_ROOT" | cksum | awk '{print $1}')"
SCHED_LOG="$AGENTS_DIR/logs/update-agents.log"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/com.$SCHED_ID.plist"

# --- schedule: Mondays 09:17 local time --------------------------------------
# launchd on macOS; on Linux a systemd user timer (catches up after downtime),
# else cron. Each install rewrites its entry, so re-running is harmless.
systemd_user_ok() { have systemctl && systemctl --user show-environment >/dev/null 2>&1; }
cron_ok() { have crontab && ! crontab -l 2>&1 | grep -qi 'not allowed'; }

install_schedule() {
  mkdir -p "$AGENTS_DIR/logs"
  local env_path="$PATH" nprefix="${N_PREFIX:-}"
  if [ "$OS_FAMILY" = macos ]; then
    mkdir -p "$(dirname "$LAUNCHD_PLIST")"
    cat > "$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.$SCHED_ID</string>
  <key>ProgramArguments</key><array><string>$REPO_ROOT/scripts/update-agents.sh</string><string>--cron</string></array>
  <key>WorkingDirectory</key><string>$REPO_ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$env_path</string></dict>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>17</integer></dict>
  <key>StandardOutPath</key><string>$SCHED_LOG</string>
  <key>StandardErrorPath</key><string>$SCHED_LOG</string>
</dict></plist>
EOF
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null; launchctl load "$LAUNCHD_PLIST" \
      && ok "weekly schedule: launchd agent com.$SCHED_ID (Mondays 09:17)" || warn "could not load the launchd agent"
  elif systemd_user_ok; then
    mkdir -p "$SYSTEMD_DIR"
    cat > "$SYSTEMD_DIR/$SCHED_ID.service" <<EOF
[Unit]
Description=Update the AI dev team tools for $REPO_ROOT

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
Environment="PATH=$env_path" "N_PREFIX=$nprefix"
ExecStart=/bin/bash -c 'exec "\$\$0" --cron >> "\$\$1" 2>&1' "$REPO_ROOT/scripts/update-agents.sh" "$SCHED_LOG"
EOF
    cat > "$SYSTEMD_DIR/$SCHED_ID.timer" <<EOF
[Unit]
Description=Weekly AI dev team tool update for $REPO_ROOT

[Timer]
OnCalendar=Mon *-*-* 09:17:00
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload && systemctl --user enable --now "$SCHED_ID.timer" >/dev/null 2>&1 \
      && ok "weekly schedule: systemd user timer $SCHED_ID.timer (Mondays 09:17; next: $(systemctl --user show "$SCHED_ID.timer" -p NextElapseUSecRealtime --value 2>/dev/null))" \
      || { warn "could not enable the systemd user timer"; return 0; }
    if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != yes ]; then
      warn "user lingering is off: the timer runs only while you have a session (missed runs catch up at the next one). For unattended runs: loginctl enable-linger $(id -un)"
    fi
  elif cron_ok; then
    local env="PATH='$env_path' HOME='$HOME'"
    [ -n "$nprefix" ] && env="$env N_PREFIX='$nprefix'"
    local line="17 9 * * 1 cd '$REPO_ROOT' && $env scripts/update-agents.sh --cron >> '$SCHED_LOG' 2>&1 $CRON_TAG"
    local current; current="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current" | grep -qxF "$line"; then ok "weekly schedule present (cron: Mondays 09:17)"; return 0; fi
    { printf '%s\n' "$current" | grep -vF "$CRON_TAG" | sed '/^$/d'; printf '%s\n' "$line"; } | crontab - \
      && ok "weekly schedule: cron (Mondays 09:17)" || warn "could not install the cron entry"
  else
    warn "no usable scheduler (launchd, systemd user, cron); run scripts/update-agents.sh --cron weekly yourself"
  fi
}

remove_schedule() {
  if [ "$OS_FAMILY" = macos ] && [ -f "$LAUNCHD_PLIST" ]; then
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null; rm -f "$LAUNCHD_PLIST"
  fi
  if [ -f "$SYSTEMD_DIR/$SCHED_ID.timer" ]; then
    systemctl --user disable --now "$SCHED_ID.timer" >/dev/null 2>&1
    rm -f "$SYSTEMD_DIR/$SCHED_ID.timer" "$SYSTEMD_DIR/$SCHED_ID.service"
    systemctl --user daemon-reload 2>/dev/null
  fi
  if cron_ok && crontab -l 2>/dev/null | grep -qF "$CRON_TAG"; then
    crontab -l 2>/dev/null | grep -vF "$CRON_TAG" | crontab -
  fi
  ok "weekly schedule removed"
}

if [ "$schedule" = remove ]; then remove_schedule; exit 0; fi
if [ "$schedule" = only ]; then install_schedule; exit 0; fi

mkdir -p "$AGENTS_DIR/backups" "$AGENTS_DIR/logs"
if ! mkdir "$AGENTS_DIR/lock" 2>/dev/null; then
  die "another update is running (remove $AGENTS_DIR/lock if it is stale)"
fi
trap 'rmdir "$AGENTS_DIR/lock" 2>/dev/null' EXIT

plugin_field() { # plugin_field <plugin@marketplace> <field>
  node -e '
    const [file, id, field] = process.argv.slice(1);
    const all = JSON.parse(require("fs").readFileSync(file, "utf8")).plugins[id] || [];
    const e = all.find((x) => x.scope === "project" && x.projectPath && require("fs").realpathSync(x.projectPath) === require("fs").realpathSync(process.cwd())) || all[0];
    console.log(e ? e[field] : "");
  ' "$PLUGINS_JSON" "$1" "$2" 2>/dev/null
}

tool_versions() {
  printf '  %-12s %s\n' gstack "$(cat "$GSTACK_DIR/VERSION" 2>/dev/null || echo MISSING) ($(git -C "$GSTACK_DIR" rev-parse --short HEAD 2>/dev/null))"
  printf '  %-12s %s\n' oh-my-claudecode "$(cd "$REPO_ROOT" && plugin_field "$OMC_PLUGIN" version)"
  printf '  %-12s %s\n' superpowers "$(cd "$REPO_ROOT" && plugin_field "$SP_PLUGIN" version)"
  printf '  %-12s %s\n' claude "$(claude --version 2>/dev/null | awk '{print $1}')"
}

# --- 1. prerequisites ---------------------------------------------------------
# Moves Node.js to the latest LTS, then checks the new binary actually starts.
# If it doesn't, it reverts to the previous version (n, nvm) so the rest of
# the toolchain keeps working.
update_node() {
  local mgr prev; mgr="$(node_manager)"; prev="$(node --version 2>/dev/null)"
  log "Node.js $prev (managed by $mgr) -> latest LTS"
  ensure_node_runtime_libs || warn "could not install libatomic; a new Node.js may not start"
  local nprefix="${N_PREFIX:-$(dirname "$(dirname "$(command -v n 2>/dev/null || echo /x/bin/n)")")}"
  case "$mgr" in
    n)     N_PREFIX="$nprefix" n lts ;;
    nvm)   load_nvm && nvm install --lts --reinstall-packages-from=current && nvm alias default 'lts/*' ;;
    volta) volta install node@lts ;;
    fnm)   fnm install --lts && fnm default lts-latest ;;
    brew)  brew upgrade node ;;
    system)
      case "$PKG" in
        apt|dnf|yum|zypper|pacman|apk) pkg_upgrade nodejs ;;
        *) return 1 ;;
      esac ;;
    none)  install_node_lts_via_nvm ;;
    *)     warn "unknown Node.js install at $(command -v node); not touching it"; return 1 ;;
  esac
  hash -r
  node_runs && return 0
  warn "Node.js $(node --version 2>&1 | head -1) does not start; reverting to $prev"
  case "$mgr" in
    n)   N_PREFIX="$nprefix" n "${prev#v}" ;;
    nvm) nvm alias default "$prev" && nvm use "$prev" ;;
  esac
  hash -r
  node_runs || warn "Node.js still does not start; fix it by hand before the next run"
  return 1
}

update_prereqs() {
  local failed=0
  update_node || { warn "Node.js update failed"; failed=1; }
  log "git, tmux -> newest packaged versions ($PKG)"
  if [ "$PKG" = brew ]; then
    local f; for f in git tmux; do brew list "$f" >/dev/null 2>&1 && { brew upgrade "$f" || failed=1; }; done
  else
    pkg_upgrade git tmux || { warn "git/tmux upgrade failed (no root?); continuing"; failed=1; }
  fi
  log "Bun $(bun_version) -> latest"
  bun upgrade || { warn "bun upgrade failed"; failed=1; }
  return $failed
}

# --- 2. snapshot ----------------------------------------------------------------
BACKUP=""
snapshot() {
  BACKUP="$AGENTS_DIR/backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP"
  git -C "$GSTACK_DIR" rev-parse HEAD > "$BACKUP/gstack.sha"
  cp "$PLUGINS_JSON" "$BACKUP/installed_plugins.json"
  # Archive the installed plugin versions relative to the cache root, so a
  # restore never has to create directories above it (e.g. an automounted /home).
  local paths=() p cache
  cache="$(cd "$PLUGIN_CACHE" && pwd -P)"
  for p in "$(cd "$REPO_ROOT" && plugin_field "$OMC_PLUGIN" installPath)" "$(cd "$REPO_ROOT" && plugin_field "$SP_PLUGIN" installPath)"; do
    [ -n "$p" ] && [ -d "$p" ] || continue
    p="$(cd "$p" && pwd -P)"
    case "$p" in "$cache"/*) paths+=("${p#"$cache"/}") ;; esac
  done
  [ ${#paths[@]} -gt 0 ] && tar -C "$cache" -cf "$BACKUP/plugin-cache.tar" "${paths[@]}"
  tar -C "$REPO_ROOT" -cf "$BACKUP/project-claude.tar" .claude CLAUDE.md
  # Keep the four most recent snapshots.
  ls -1d "$AGENTS_DIR"/backups/*/ 2>/dev/null | sort -r | tail -n +5 | while read -r d; do rm -rf "$d"; done
  ok "snapshot saved to ${BACKUP#"$REPO_ROOT"/}"
}

# --- 3. update the tools --------------------------------------------------------
update_tools() {
  local failed=0 root

  log "gstack -> tip of its release channel (bin/gstack-upgrade)"
  # Run from the checkout itself so gstack-upgrade doesn't certify this repo.
  # Exit 3 is a refusal that changed nothing (e.g. a live session holds the
  # install); the installed version stays and next week's run retries.
  (cd "$GSTACK_DIR" && ./bin/gstack-upgrade) 2>&1 | grep -v BASH_COMPAT
  case "${PIPESTATUS[0]}" in
    0) ;;
    3) warn "gstack upgrade refused (nothing changed); it will be retried next run" ;;
    *) warn "gstack upgrade failed"; failed=1 ;;
  esac

  log "oh-my-claudecode -> latest (marketplace update, plugin update, project setup)"
  (cd "$REPO_ROOT" && claude plugin marketplace update omc && claude plugin update "$OMC_PLUGIN" --scope project) \
    || { warn "OMC plugin update failed"; failed=1; }
  root="$(cd "$REPO_ROOT" && plugin_field "$OMC_PLUGIN" installPath)"
  if [ -f "$root/scripts/setup-claude-md.sh" ]; then
    (cd "$REPO_ROOT" && OMC_SETUP_PLUGIN_ROOT="$root" bash "$root/scripts/setup-claude-md.sh" local) >/dev/null \
      || { warn "OMC project setup (omc-setup --local) failed"; failed=1; }
  fi

  log "Superpowers -> latest (marketplace update, plugin update)"
  (cd "$REPO_ROOT" && claude plugin marketplace update claude-plugins-official && claude plugin update "$SP_PLUGIN" --scope project) \
    || { warn "Superpowers plugin update failed"; failed=1; }
  return $failed
}

# --- 4. rollback ----------------------------------------------------------------
rollback() {
  warn "rolling the tools back to ${BACKUP#"$REPO_ROOT"/}"
  local old; old="$(cat "$BACKUP/gstack.sha")"
  if [ "$(git -C "$GSTACK_DIR" rev-parse HEAD)" != "$old" ]; then
    git -C "$GSTACK_DIR" reset -q --hard "$old" \
      && (cd "$GSTACK_DIR" && ./setup --team --no-skill-routing -q < /dev/null) >/dev/null 2>&1 \
      || warn "gstack rollback did not complete; fix: cd $GSTACK_DIR && git reset --hard $old && ./setup --team"
  fi
  # Hold gstack's own session-start auto-update, or the next session would
  # pull the release that just failed. The next successful run lifts it.
  "$GSTACK_DIR/bin/gstack-config" set auto_upgrade false >/dev/null 2>&1 && touch "$GSTACK_HOLD"
  [ -f "$BACKUP/plugin-cache.tar" ] && tar -C "$PLUGIN_CACHE" -xf "$BACKUP/plugin-cache.tar"
  cp "$BACKUP/installed_plugins.json" "$PLUGINS_JSON"
  tar -C "$REPO_ROOT" -xf "$BACKUP/project-claude.tar"
}

# --- run ------------------------------------------------------------------------
printf '%supdate-agents%s %s  (%s)\n' "$C_BOLD" "$C_OFF" "$(date '+%Y-%m-%d %H:%M:%S')" "$OS_PRETTY"
printf '\nBefore\n'; print_prereq_versions; tool_versions

prereq_status=0
if [ $skip_prereqs = 0 ]; then
  update_prereqs || prereq_status=1
fi

snapshot
update_tools; update_status=$?

log "smoke test"
"$REPO_ROOT/scripts/smoke-test.sh"; smoke_status=$?
[ $simulate_failure = 1 ] && { warn "--simulate-failure: treating the smoke test as failed"; smoke_status=1; }

if [ $smoke_status != 0 ]; then
  rollback
  log "smoke test after rollback"
  "$REPO_ROOT/scripts/smoke-test.sh" >/dev/null 2>&1 \
    && ok "rolled-back tools pass the smoke test" \
    || warn "rolled-back tools still fail the smoke test; see the output above"
  printf '\nAfter rollback\n'; print_prereq_versions; tool_versions
  [ "$schedule" = 1 ] && install_schedule
  printf '\n%sUpdate FAILED; tools rolled back.%s gstack auto-update is held until the next successful run.\n' "$C_RED" "$C_OFF"
  exit 1
fi

if [ -f "$GSTACK_HOLD" ]; then
  "$GSTACK_DIR/bin/gstack-config" set auto_upgrade true >/dev/null 2>&1 && rm -f "$GSTACK_HOLD" \
    && ok "gstack session auto-update re-enabled"
fi
[ "$schedule" = 1 ] && install_schedule

printf '\nAfter\n'; print_prereq_versions; tool_versions
if [ $update_status = 0 ] && [ $prereq_status = 0 ]; then
  printf '\n%sAll prerequisites and tools are up to date and pass the smoke test.%s\n' "$C_GREEN" "$C_OFF"
else
  [ $update_status = 0 ] || warn "a tool update step failed; the installed versions still pass the smoke test"
  [ $prereq_status = 0 ] || warn "a prerequisite update step failed; see the warnings above"
  printf '\n%sInstalled versions pass the smoke test, but not every update applied (see warnings).%s\n' "$C_YELLOW" "$C_OFF"
fi
