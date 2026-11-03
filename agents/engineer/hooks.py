"""Engineer pre-tool-use checks: full shell in the workspace, minus pushes, protected paths and secrets."""

from agents._shared.guards import check_bash_common, check_file_write


def pre_tool_use(tool: str, tool_input: dict) -> str | None:
    if tool == "Bash":
        return check_bash_common(tool_input.get("command", ""))
    return check_file_write(tool, tool_input)
