#!/usr/bin/env bash
# Launch the /devteam pipeline in Claude Code.
#
#   scripts/devteam.sh <goal-file | "goal text"> [--dry-run] [--new] [-- <claude args>]
#   scripts/devteam.sh                 resume the run in .devteam/state.json
#   scripts/devteam.sh --status        show the run's progress
#
# An argument naming an existing file is read as the goal; anything else is
# the goal text itself. Either way the goal lands in .devteam/goal.md.
#   --dry-run   print every phase in order with its owning tool; change nothing
#   --new       archive the current run (.devteam/archive/) and start over
#   --prepare   intake only, no launch (what /devteam itself calls)
#   -- ...      extra arguments for claude, e.g. -- --model sonnet
set -euo pipefail
. "$(dirname "$0")/lib/common.sh"
add_user_tool_paths

DT_JS="$REPO_ROOT/scripts/lib/devteam.mjs"
DT_DIR="$REPO_ROOT/.devteam"
GOAL_MD="$DT_DIR/goal.md"

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit "${1:-0}"; }

dry_run=0 new=0 prepare=0 show_status=0 goal_arg="" have_goal=0
claude_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --new) new=1 ;;
    --prepare) prepare=1 ;;
    --status) show_status=1 ;;
    -h|--help) usage ;;
    --) shift; claude_args=("$@"); break ;;
    *)
      [ $have_goal = 0 ] || die "one goal argument only (quote goal text: scripts/devteam.sh \"a todo app\")"
      goal_arg="$1"; have_goal=1 ;;
  esac
  shift
done

have node || die "Node.js is required (run scripts/setup-prereqs.sh)"
[ $show_status = 1 ] && exec node "$DT_JS" status

# Relative goal files resolve against the caller's directory, then the repo.
goal_file=""
if [ $have_goal = 1 ] && [ -n "$goal_arg" ]; then
  for cand in "$goal_arg" "$REPO_ROOT/$goal_arg"; do
    [ -f "$cand" ] && { goal_file="$(cd "$(dirname "$cand")" && pwd)/$(basename "$cand")"; break; }
  done
  if [ -z "$goal_file" ] && [[ "$goal_arg" =~ ^[^[:space:]]+\.(md|markdown|txt)$ ]]; then
    warn "no file named '$goal_arg'; treating it as goal text"
  fi
fi

if [ $dry_run = 1 ]; then
  printf '%s/devteam dry run%s: nothing is written and Claude Code is not started\n\n' "$C_BOLD" "$C_OFF"
  if [ -n "$goal_file" ]; then
    printf 'Goal      file %s (read, copied to .devteam/goal.md)\n' "${goal_file#"$REPO_ROOT"/}"
    crit="$(node "$DT_JS" critical "$goal_file")"
  elif [ $have_goal = 1 ]; then
    printf 'Goal      text "%s" (saved to .devteam/goal.md)\n' "$goal_arg"
    crit=""
  elif [ -f "$DT_DIR/state.json" ]; then
    printf 'Goal      resume the run in .devteam/state.json at "%s"\n' "$(node "$DT_JS" next)"
    crit="$(node "$DT_JS" critical "$GOAL_MD")"
  else
    die "no goal given and no run to resume"
  fi
  if [ -n "$crit" ]; then
    printf 'Critical  from the goal'"'"'s "Critical features" section (%d):\n' "$(printf '%s\n' "$crit" | wc -l)"
    printf '%s\n' "$crit" | sed 's/^/            - /'
  else
    printf 'Critical  none listed in the goal; inferred from the CLAUDE.md criteria and shown for approval\n'
  fi
  if [ -f "$DT_DIR/state.json" ] && [ $have_goal = 1 ] && [ $new = 0 ]; then
    printf 'State     a run already exists; it resumes if this is the same goal, otherwise pass --new\n'
  fi
  launch="claude \"/devteam${goal_arg:+ $goal_arg}\"${claude_args[*]:+ ${claude_args[*]}}"
  printf 'Launch    %s\n\n' "$launch"
  printf '%s %-3s %-16s %-12s %s%s\n' "$C_BOLD" "#" "Phase" "Owner" "What happens" "$C_OFF"
  n=0
  while IFS=$'\t' read -r id owner cmds kind summary; do
    n=$((n + 1))
    if [ "$kind" = gate ]; then
      printf '  %-3s %-16s %-12s %s\n' "$n" "$id" "$owner" "PAUSE for your approval of $summary"
    else
      printf '  %-3s %-16s %-12s %s\n' "$n" "$id" "$owner" "$summary"
      [ "$cmds" = - ] || printf '  %-3s %-16s %-12s %s\n' "" "" "" "  via ${cmds// /, }"
    fi
  done < <(node "$DT_JS" phases)
  have claude || warn "claude is not on PATH; install Claude Code before a real run"
  exit 0
fi

# --- intake -----------------------------------------------------------------
mkdir -p "$DT_DIR"
[ $new = 1 ] && node "$DT_JS" archive

if [ $have_goal = 1 ]; then
  [ -n "$goal_arg" ] || die "goal text is empty"
  tmp="$(mktemp)"
  if [ -n "$goal_file" ]; then
    cp "$goal_file" "$tmp"; source_desc="file:${goal_file#"$REPO_ROOT"/}"
  else
    printf '# Goal\n\n%s\n' "$goal_arg" > "$tmp"; source_desc="text"
  fi
  # A different goal must not overwrite the goal of a run that is in flight.
  if [ -f "$DT_DIR/state.json" ] && [ -f "$GOAL_MD" ] && ! cmp -s "$tmp" "$GOAL_MD"; then
    rm -f "$tmp"
    die "a run for a different goal is in progress ($(node "$DT_JS" next)); resume it with scripts/devteam.sh, or pass --new to archive it"
  fi
  mv "$tmp" "$GOAL_MD"
  result="$(node "$DT_JS" init "$GOAL_MD" "$source_desc")"
elif [ -f "$DT_DIR/state.json" ]; then
  result="resume $(node "$DT_JS" next)"
else
  usage 1
fi

case "$result" in
  new*)    ok "new run; goal saved to .devteam/goal.md" ;;
  resume*) ok "resuming run at phase: ${result#resume }" ;;
esac
node "$DT_JS" status

[ $prepare = 1 ] && exit 0
have claude || die "claude is not on PATH (https://claude.com/claude-code)"
cd "$REPO_ROOT"
exec claude "${claude_args[@]}" "/devteam${goal_arg:+ $goal_arg}"
