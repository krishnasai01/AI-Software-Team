# Shared helpers for the devteam scripts. Source it; don't run it.
# shellcheck shell=bash

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export REPO_ROOT

# Pinned by gstack's setup: it refuses Bun older than 1.3.3.
BUN_MIN_VERSION="1.3.3"
NODE_MIN_MAJOR=20

if [ -t 1 ]; then
  C_BLUE=$'\033[34m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_OFF=""
fi

log()  { printf '%s==>%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
ok()   { printf '%s ok%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Tools installed per-user (Bun, nvm, n, Homebrew) are often not on PATH in
# cron or a fresh non-login shell, so put their usual homes first.
add_user_tool_paths() {
  local d
  for d in "$HOME/.bun/bin" "${N_PREFIX:-$HOME/n}/bin" "$HOME/.local/bin" \
           /opt/homebrew/bin /usr/local/bin; do
    [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac
  done
  export PATH
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
}

# Sets OS_FAMILY (macos|debian|rhel|fedora|arch|suse|alpine|windows|unknown)
# and PKG (brew|apt|dnf|yum|pacman|zypper|apk|"").
detect_os() {
  OS_FAMILY=unknown; PKG=""; OS_PRETTY="$(uname -s)"
  case "$(uname -s)" in
    Darwin)
      OS_FAMILY=macos; OS_PRETTY="macOS $(sw_vers -productVersion 2>/dev/null)"
      have brew && PKG=brew ;;
    Linux)
      local id="" like=""
      if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        id="${ID:-}"; like="${ID_LIKE:-}"; OS_PRETTY="${PRETTY_NAME:-Linux}"
      fi
      grep -qi microsoft /proc/version 2>/dev/null && OS_PRETTY="$OS_PRETTY (WSL)"
      # RHEL clones list "fedora" in ID_LIKE, so test them before Fedora.
      case " $id $like " in
        *" debian "*|*" ubuntu "*) OS_FAMILY=debian; PKG=apt ;;
        *" rhel "*|*" centos "*|*" rocky "*|*" almalinux "*|*" ol "*|*" amzn "*)
          OS_FAMILY=rhel; if have dnf; then PKG=dnf; else PKG=yum; fi ;;
        *" fedora "*)              OS_FAMILY=fedora; PKG=dnf ;;
        *" arch "*)                OS_FAMILY=arch; PKG=pacman ;;
        *" suse "*|*" opensuse "*) OS_FAMILY=suse; PKG=zypper ;;
        *" alpine "*)              OS_FAMILY=alpine; PKG=apk ;;
      esac ;;
    MINGW*|MSYS*|CYGWIN*) OS_FAMILY=windows; OS_PRETTY="Windows ($(uname -s))" ;;
  esac
}

# Root runs directly; otherwise sudo. NONINTERACTIVE=1 (cron) never prompts
# for a password and fails instead.
as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"
  elif have sudo; then
    if [ "${NONINTERACTIVE:-0}" = 1 ]; then sudo -n "$@"; else sudo "$@"; fi
  else
    warn "need root for: $* (no sudo available)"; return 1
  fi
}

# pkg_install <package...> — install with the system package manager.
pkg_install() {
  case "$PKG" in
    brew)   brew install "$@" ;;
    apt)    as_root apt-get update -y && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    dnf)    as_root dnf install -y "$@" ;;
    yum)    as_root yum install -y "$@" ;;
    pacman) as_root pacman -S --needed --noconfirm "$@" ;;
    zypper) as_root zypper --non-interactive install "$@" ;;
    apk)    as_root apk add "$@" ;;
    *)      warn "no supported package manager found to install: $*"; return 1 ;;
  esac
}

# pkg_upgrade <package...> — upgrade to the newest version the repos offer.
pkg_upgrade() {
  case "$PKG" in
    brew)   brew upgrade "$@" 2>&1 | grep -v 'already installed' || true ;;
    apt)    as_root apt-get update -y && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade "$@" ;;
    dnf)    as_root dnf upgrade -y "$@" ;;
    yum)    as_root yum update -y "$@" ;;
    pacman) as_root pacman -S --needed --noconfirm "$@" ;;
    zypper) as_root zypper --non-interactive update "$@" ;;
    apk)    as_root apk upgrade "$@" ;;
    *)      warn "no supported package manager found to upgrade: $*"; return 1 ;;
  esac
}

# version_ge A B — true when dotted version A >= B.
version_ge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$2" ]
}

node_major() { node -p 'process.versions.node.split(".")[0]' 2>/dev/null; }
node_is_lts() { [ "$(node -p 'process.release.lts ? 1 : 0' 2>/dev/null)" = 1 ]; }
bun_version() { bun --version 2>/dev/null; }

# How Node.js was installed decides how it is upgraded.
node_manager() {
  local p; p="$(command -v node 2>/dev/null)" || { echo none; return; }
  case "$p" in
    "$NVM_DIR"/*|*/.nvm/*)          echo nvm ;;
    "${N_PREFIX:-$HOME/n}"/*)       echo n ;;
    */.volta/*)                     echo volta ;;
    */.local/share/fnm/*|*/fnm_multishells/*) echo fnm ;;
    /opt/homebrew/*|/usr/local/Cellar/*|/usr/local/opt/*) echo brew ;;
    /usr/bin/*|/bin/*)              echo system ;;
    *)                              echo unknown ;;
  esac
}

# Official Node.js Linux binaries (v26+) link against libatomic, which minimal
# RHEL/Debian installs lack. Without it node fails with "libatomic.so.1: cannot
# open shared object file".
ensure_node_runtime_libs() {
  [ "$(uname -s)" = Linux ] || return 0
  if have ldconfig && ldconfig -p 2>/dev/null | grep -q 'libatomic\.so\.1'; then return 0; fi
  case "$OS_FAMILY" in
    rhel|fedora) pkg_install libatomic ;;
    debian|suse) pkg_install libatomic1 ;;
    alpine)      pkg_install libatomic ;;
    *)           return 0 ;;
  esac
}

node_runs() { node -e 'process.exit(0)' >/dev/null 2>&1; }

load_nvm() { [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1; have nvm; }

# Official nvm installer, then the current LTS. Works without root on every
# Unix, which is why it is used instead of distro Node packages (often old).
install_node_lts_via_nvm() {
  ensure_node_runtime_libs || warn "could not install libatomic; Node.js may not start"
  if ! load_nvm; then
    local tag
    tag="$(curl -fsSL https://api.github.com/repos/nvm-sh/nvm/releases/latest | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p')"
    [ -n "$tag" ] || tag="v0.40.3"
    log "installing nvm $tag (official installer)"
    curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/$tag/install.sh" | PROFILE=/dev/null bash
    load_nvm || die "nvm install failed"
  fi
  nvm install --lts && nvm alias default 'lts/*' >/dev/null
}

# Official Bun installer (https://bun.sh). Needs unzip.
install_bun() {
  have unzip || pkg_install unzip || die "Bun's installer needs unzip"
  log "installing Bun (official installer)"
  curl -fsSL https://bun.sh/install | bash
  add_user_tool_paths
  have bun || die "Bun install finished but bun is not on PATH"
}

# gstack's /cso builds a native helper and needs a static-capable C compiler on
# Linux (Xcode command-line tools on macOS). Not a hard prerequisite: without
# it gstack still installs and /cso reports "not assessed".
cc_static_ok() {
  have cc || return 1
  local t; t="$(mktemp -d)"
  printf 'int main(void){return 0;}\n' > "$t/p.c"
  if [ "$OS_FAMILY" = macos ]; then cc "$t/p.c" -o "$t/p" >/dev/null 2>&1
  else cc -std=c11 -static "$t/p.c" -o "$t/p" >/dev/null 2>&1; fi
  local rc=$?; rm -rf "$t"; return $rc
}

install_c_toolchain() {
  case "$OS_FAMILY" in
    macos)       xcode-select --install 2>/dev/null; warn "finish the Xcode command-line tools dialog, then re-run"; return 1 ;;
    debian)      pkg_install build-essential ;;
    rhel|fedora) pkg_install gcc glibc-static ;;
    arch)        pkg_install base-devel ;;
    suse)        pkg_install gcc glibc-devel-static ;;
    alpine)      pkg_install build-base ;;
    *)           return 1 ;;
  esac
}

prereq_version() {
  have "$1" || { echo MISSING; return 1; }
  local v
  case "$1" in
    node) v="$(node --version 2>/dev/null)" && v="$v$(node_is_lts && echo ' (LTS)')" ;;
    npm)  v="$(npm --version 2>/dev/null)" ;;
    git)  v="$(git --version 2>/dev/null)" && v="$(echo "$v" | awk '{print $3}')" ;;
    bun)  v="$(bun --version 2>/dev/null)" ;;
    tmux) v="$(tmux -V 2>/dev/null)" && v="$(echo "$v" | awk '{print $2}')" ;;
    cc)   v="$(cc --version 2>/dev/null | head -1)" ;;
  esac
  [ -n "$v" ] || { echo "BROKEN (installed but does not start)"; return 1; }
  echo "$v"
}

# Prints every prerequisite's version; returns non-zero if a required one is
# missing. cc is optional (gstack /cso only).
print_prereq_versions() {
  local t v rc=0
  printf '\n%sPrerequisite versions%s\n' "$C_BOLD" "$C_OFF"
  for t in node npm git bun tmux; do
    v="$(prereq_version "$t")" || rc=1
    printf '  %-8s %s\n' "$t" "$v"
  done
  printf '  %-8s %s\n' "cc" "$(prereq_version cc) (optional, for gstack /cso)"
  return $rc
}
