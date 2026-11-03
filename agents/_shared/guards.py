"""Pre-tool-use checks shared by all roles. Pure stdlib: this runs inside the sandbox.

Hooks are the first line of defense. They are best effort against shell tricks, which is why the
runner re-checks the final diff (`protected_paths_touched`) before pushing, and why branch
protection, CODEOWNERS and App permissions sit behind both.

Each check returns a reason string to deny, or None to allow.
"""

import re
import shlex
from fnmatch import fnmatch
from pathlib import PurePosixPath

WORKSPACE = "/workspace"

# Paths agents never modify. CI, infra and secrets are human-only (also enforced by CODEOWNERS).
PROTECTED_GLOBS = [
    ".github/*",
    ".github/**",
    "infra/*",
    "infra/**",
    ".claude/*",
    ".claude/**",
    "**/.env",
    ".env",
    "**/.env.*",
    ".env.*",
    "**/*.pem",
    "*.pem",
    "**/*.key",
    "*.key",
    "**/secrets/**",
    "secrets/**",
    ".sops.yaml",
    "CODEOWNERS",
    "**/CODEOWNERS",
]

FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

_SECRET_READS = re.compile(
    r"(^|[\s;&|(])(env|printenv|set)(\s*$|\s*[;&|)])|/proc/[^\s]*/environ|\.git-credentials|"
    r"credential\.helper|\$\{?(GH_TOKEN|GITHUB_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN)\b"
)
_NETWORK_TOOLS = re.compile(r"(^|[\s;&|(])(curl|wget|nc|ncat|telnet|ssh|scp|rsync|ftp)\s")


def relative_to_workspace(path: str, cwd: str = WORKSPACE) -> str | None:
    """Normalize a path; None if it escapes the workspace."""
    p = PurePosixPath(path if path.startswith("/") else f"{cwd}/{path}")
    parts: list[str] = []
    for part in p.parts[1:]:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    norm = "/" + "/".join(parts)
    if norm == WORKSPACE:
        return ""
    if not norm.startswith(WORKSPACE + "/"):
        return None
    return norm[len(WORKSPACE) + 1 :]


def is_protected(rel_path: str) -> bool:
    return any(fnmatch(rel_path, g) for g in PROTECTED_GLOBS)


def protected_paths_touched(changed_files: list[str]) -> list[str]:
    return [f for f in changed_files if is_protected(f)]


def check_file_write(tool: str, tool_input: dict) -> str | None:
    if tool not in FILE_WRITE_TOOLS:
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    rel = relative_to_workspace(path)
    if rel is None:
        return f"writes outside {WORKSPACE} are not allowed ({path})"
    if is_protected(rel):
        return f"{rel} is a protected path (CI, infra, secrets or agent settings); ask a human instead"
    return None


def _segments(command: str) -> list[list[str]]:
    """Split a shell command on ; && || | and newlines into token lists (best effort)."""
    out: list[list[str]] = []
    for chunk in re.split(r"&&|\|\||[;|\n]", command):
        try:
            tokens = shlex.split(chunk)
        except ValueError:
            tokens = chunk.split()
        if tokens:
            out.append(tokens)
    return out


def check_bash_common(command: str) -> str | None:
    if _SECRET_READS.search(command):
        return "reading credentials or the environment is not allowed"
    if _NETWORK_TOOLS.search(command):
        return "direct network tools are not allowed; package managers go through the egress proxy"
    for tokens in _segments(command):
        # Skip leading env assignments and sudo-like wrappers.
        while tokens and ("=" in tokens[0] and not tokens[0].startswith("-")):
            tokens = tokens[1:]
        if not tokens:
            continue
        prog = PurePosixPath(tokens[0]).name
        if prog in ("sudo", "su", "doas"):
            return "privilege escalation is not allowed"
        if prog == "git":
            reason = _check_git(tokens[1:])
            if reason:
                return reason
        if prog in ("rm", "rmdir", "shred", "unlink", "mv", "chmod", "chown", "truncate", "dd"):
            for target in tokens[1:]:
                if target.startswith("-"):
                    continue
                if prog == "dd":
                    if not target.startswith("of="):
                        continue
                    target = target.removeprefix("of=")
                if target.startswith("~"):
                    return f"{prog} outside {WORKSPACE} is not allowed ({target})"
                rel = relative_to_workspace(target)
                if rel is None:
                    return f"{prog} outside {WORKSPACE} is not allowed ({target})"
                if rel == "" or rel == ".git" or rel.startswith(".git/"):
                    return f"{prog} on the workspace root or .git is not allowed"
                if is_protected(rel):
                    return f"{rel} is a protected path"
    # Shell writes into protected paths (redirects, sed -i, tee, cp, ...).
    for glob_root in (".github/", "infra/", ".claude/", ".env", "secrets/", "CODEOWNERS", ".sops.yaml"):
        if glob_root in command and re.search(r"(>|\btee\b|\bsed\s+-i|\bcp\b|\bmv\b|\binstall\b|\bln\b)", command):
            return f"modifying {glob_root} from the shell is not allowed"
    return None


# Global git options that consume the next argument (`git -c k=v push`, `git -C dir push`).
_GIT_OPTS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}


def git_subcommand(args: list[str]) -> str:
    """The subcommand of a git invocation, skipping global options. `args` excludes `git` itself."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GIT_OPTS_WITH_VALUE:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a
    return ""


def _check_git(args: list[str]) -> str | None:
    sub = git_subcommand(args)
    if sub == "push":
        return "the runner pushes your branch when you finish; do not push yourself"
    if sub in ("remote", "config") and any(a in args for a in ("set-url", "add", "--add", "url", "credential.helper")):
        return "changing remotes or git credentials is not allowed"
    if sub == "reset" and "--hard" in args and any("origin" in a or "@{u}" in a for a in args):
        return "resetting to a remote ref is not allowed; make a new commit instead"
    if sub in ("filter-branch", "filter-repo", "update-ref", "replace", "gc", "prune", "reflog"):
        return f"git {sub} is not allowed"
    if sub == "branch" and any(a in ("-D", "-d", "--delete", "-M", "-m") for a in args):
        return "deleting or renaming branches is not allowed"
    if sub == "checkout" and any(a in ("--orphan",) for a in args):
        return "orphan branches are not allowed"
    return None
