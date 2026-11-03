"""Engineer pre-tool-use hints: full shell in the workspace; protected paths and history rewrites are
flagged early because the worker would reject them anyway."""

from agents._shared.guards import check_engineer_bash, check_file_write


def pre_tool_use(tool: str, tool_input: dict) -> str | None:
    if tool == "Bash":
        return check_engineer_bash(tool_input.get("command", ""))
    return check_file_write(tool, tool_input)
