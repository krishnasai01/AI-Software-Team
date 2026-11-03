"""Reviewer pre-tool-use checks: read-only. The shell may only run inspection commands."""

import shlex

from agents._shared.guards import _segments, check_bash_common, git_subcommand

READ_ONLY_PROGRAMS = {"ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree", "file", "stat", "diff"}
READ_ONLY_GIT = {"diff", "log", "show", "status", "blame", "ls-files", "rev-parse", "grep", "shortlog", "merge-base"}


def pre_tool_use(tool: str, tool_input: dict) -> str | None:
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "the reviewer is read-only; put requested changes in your review"
    if tool != "Bash":
        return None
    command = tool_input.get("command", "")
    if any(op in command for op in (">", "$(", "`")):
        return "redirects and command substitution are not allowed for the reviewer"
    reason = check_bash_common(command)
    if reason:
        return reason
    for tokens in _segments(command):
        prog = tokens[0]
        if prog == "git":
            sub = git_subcommand(tokens[1:])
            if sub not in READ_ONLY_GIT:
                return f"git {sub} is not a read-only command"
        elif prog == "find":
            if any(t in ("-exec", "-execdir", "-delete", "-ok", "-fprint") for t in tokens):
                return "find may only list files"
        elif prog not in READ_ONLY_PROGRAMS:
            return f"{shlex.quote(prog)} is not on the reviewer's read-only allowlist"
    return None
