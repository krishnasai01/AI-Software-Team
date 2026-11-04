"""Tech lead pre-tool-use checks: read-only. The tech lead decides; engineers change code."""

from agents._shared.guards import check_read_only as pre_tool_use

__all__ = ["pre_tool_use"]
