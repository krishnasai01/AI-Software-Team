"""Pre-tool-use hints shared by all roles. Pure stdlib: this runs inside the sandbox.

These are early feedback, not the security boundary. The sandbox holds no GitHub credentials, and
the worker re-checks every result in its own clean repository (`core/gitops.py`) before pushing.
So the hooks only stop actions that would certainly be rejected later, saving the agent a wasted
run; they don't try to police a disposable container.
"""

import re
import shlex
from fnmatch import fnmatch
from pathlib import PurePosixPath

WORKSPACE = "/workspace"

# Paths agents never change. CI, infra, secrets and everything that steers the agents themselves
# (instructions, brain, policy) belong to a human. The worker refuses to push changes to them.
PROTECTED_GLOBS = [
    ".github/**",
    "infra/**",
    ".claude/**",
    ".agent-company/**",
    "docs/brain/**",
    "CLAUDE.md",
    "**/CLAUDE.md",
    ".mcp.json",
    ".env",
    "**/.env",
    ".env.*",
    "**/.env.*",
    "*.pem",
    "**/*.pem",
    "*.key",
    "**/*.key",
    "secrets/**",
    "**/secrets/**",
    ".sops.yaml",
    "CODEOWNERS",
    "**/CODEOWNERS",
]

FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_PROTECTED_ROOTS = (".github/", "infra/", ".claude/", ".agent-company/", "docs/brain/", "CLAUDE.md", "CODEOWNERS")


def path_matches(path: str, pattern: str) -> bool:
    """fnmatch where a leading `**/` also matches at the top level."""
    return fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch(path, pattern[3:]))


def is_protected(rel_path: str) -> bool:
    return any(path_matches(rel_path, g) for g in PROTECTED_GLOBS)


def protected_paths_touched(changed_files: list[str]) -> list[str]:
    return [f for f in changed_files if is_protected(f)]


def relative_to_workspace(path: str, cwd: str = WORKSPACE) -> str | None:
    """Normalize a path; None if it is outside the workspace."""
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


def check_file_write(tool: str, tool_input: dict) -> str | None:
    if tool not in FILE_WRITE_TOOLS:
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    rel = relative_to_workspace(path)
    if rel is not None and is_protected(rel):
        return (
            f"{rel} is a protected path (CI, infra, secrets, or agent instructions). Changes there "
            "are never pushed; finish without it and list what is needed in open_questions."
        )
    return None


def segments(command: str) -> list[list[str]]:
    """Split a shell command on ; && || | and newlines into token lists (best effort)."""
    out: list[list[str]] = []
    for chunk in re.split(r"&&|\|\||[;|\n]", command):
        try:
            tokens = shlex.split(chunk)
        except ValueError:
            tokens = chunk.split()
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens = tokens[1:]  # leading VAR=value assignments
        if tokens:
            out.append(tokens)
    return out


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


def check_git(args: list[str]) -> str | None:
    sub = git_subcommand(args)
    if sub == "push":
        return "there is no remote to push to; commit your work and the platform hands it off when you finish"
    if sub in ("rebase", "filter-branch", "filter-repo", "replace"):
        return f"git {sub} rewrites history, and rewritten history is rejected; add new commits instead"
    if sub == "reset" and "--hard" in args and any("origin" in a or "@{u}" in a or "~" in a for a in args):
        return "resetting away existing commits is rejected; add a new commit that undoes the change instead"
    if sub == "checkout" and "--orphan" in args:
        return "orphan branches are rejected"
    return None


def check_engineer_bash(command: str) -> str | None:
    for tokens in segments(command):
        if PurePosixPath(tokens[0]).name == "git":
            reason = check_git(tokens[1:])
            if reason:
                return reason
    if any(root in command for root in _PROTECTED_ROOTS) and re.search(
        r"(>|\btee\b|\bsed\s+-i|\bcp\b|\bmv\b|\brm\b|\binstall\b|\bln\b)", command
    ):
        return "that command modifies a protected path; such changes are never pushed"
    return None


READ_ONLY_PROGRAMS = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "rg",
    "find",
    "tree",
    "file",
    "stat",
    "diff",
    "sort",
    "uniq",
}
READ_ONLY_GIT = {"diff", "log", "show", "status", "blame", "ls-files", "rev-parse", "grep", "shortlog", "merge-base"}


def check_read_only(tool: str, tool_input: dict) -> str | None:
    """For roles that only inspect the code (reviewer, tech lead)."""
    if tool in FILE_WRITE_TOOLS:
        return "this role is read-only; put what should change in your output"
    if tool != "Bash":
        return None
    command = tool_input.get("command", "")
    if any(op in command for op in (">", "$(", "`")):
        return "redirects and command substitution are not available to this role"
    for tokens in segments(command):
        prog = PurePosixPath(tokens[0]).name
        if prog == "git":
            sub = git_subcommand(tokens[1:])
            if sub not in READ_ONLY_GIT:
                return f"git {sub} is not a read-only command"
        elif prog == "find":
            if any(t in ("-exec", "-execdir", "-delete", "-ok", "-fprint") for t in tokens):
                return "find may only list files"
        elif prog not in READ_ONLY_PROGRAMS:
            return f"{prog} is not on the read-only allowlist"
    return None
