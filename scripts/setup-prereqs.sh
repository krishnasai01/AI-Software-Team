#!/usr/bin/env bash
# Installs whatever is missing of: Node.js LTS, git, Bun, tmux.
# Uses each tool's official installer (nvm for Node, bun.sh for Bun) or the
# system package manager (git, tmux), then prints every version.
#
# Usage: scripts/setup-prereqs.sh
set -euo pipefail
. "$(dirname "$0")/lib/common.sh"

add_user_tool_paths
detect_os
log "OS: $OS_PRETTY (family=$OS_FAMILY, package manager=${PKG:-none})"

if [ "$OS_FAMILY" = windows ]; then
  cat >&2 <<'EOF'
Native Windows shells are not supported by this script. Either run it inside
WSL, or install the prerequisites from PowerShell and re-run:
  winget install OpenJS.NodeJS.LTS Git.Git Oven-sh.Bun psmux
EOF
  exit 1
fi

if [ "$OS_FAMILY" = macos ] && [ -z "$PKG" ]; then
  log "installing Homebrew (official installer)"
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  add_user_tool_paths
  have brew && PKG=brew || die "Homebrew install failed"
fi

# --- git (system package manager) -------------------------------------------
if have git; then ok "git present"
else log "installing git"; pkg_install git || die "could not install git"
fi

# --- Node.js LTS (nvm, the installer nodejs.org documents) -------------------
if ! have node; then
  log "Node.js missing; installing the current LTS"
  install_node_lts_via_nvm
elif [ "$(node_major)" -lt "$NODE_MIN_MAJOR" ]; then
  log "Node.js $(node --version) is older than $NODE_MIN_MAJOR; installing the current LTS"
  install_node_lts_via_nvm
elif ! node_is_lts; then
  warn "Node.js $(node --version) is not an LTS release; leaving it in place (run scripts/update-agents.sh to move to LTS)"
else
  ok "Node.js LTS present"
fi
ensure_node_runtime_libs || warn "could not install libatomic (needed by Node.js 26+ binaries)"

# --- Bun (official installer, https://bun.sh) --------------------------------
if ! have bun; then
  install_bun
elif ! version_ge "$(bun_version)" "$BUN_MIN_VERSION"; then
  log "Bun $(bun_version) is older than $BUN_MIN_VERSION (gstack's minimum); upgrading"
  bun upgrade
else
  ok "Bun present"
fi

# --- tmux (system package manager) ------------------------------------------
if have tmux; then ok "tmux present"
else log "installing tmux"; pkg_install tmux || die "could not install tmux"
fi

# --- C toolchain (optional; gstack /cso builds a native helper) --------------
if cc_static_ok; then ok "static-capable C compiler present"
else
  log "installing a C toolchain for gstack /cso"
  install_c_toolchain && cc_static_ok \
    || warn "no static-capable C compiler; gstack /cso will report 'not assessed' until one is installed"
fi

print_prereq_versions || die "some prerequisites are still missing"

case "${SHELL:-}" in
  */tcsh|*/csh)
    printf '\nNote: Bun'"'"'s installer only edits bash/zsh/fish rc files. For %s add:\n  setenv PATH "$HOME/.bun/bin:$PATH"\nto ~/.cshrc (or ~/.tcshrc).\n' "$SHELL" ;;
esac
