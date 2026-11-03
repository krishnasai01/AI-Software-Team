"""Model access. The only place the Anthropic SDKs are imported.

- `ask_model()`: a single completion (control side), through the proxy.
- `run_agent()`: one Claude Agent SDK session. It runs *inside* a sandbox, called by the runner.
- `LiteLLM`: per-role teams with daily budgets and per-run keys inside them. The proxy sees every
  request, so it is the hard spending limit; concurrent runs cannot overshoot it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.settings import get_settings


async def ask_model(prompt: str, model: str = "claude-haiku-5-5", system: str = "", max_tokens: int = 2048) -> str:
    from anthropic import AsyncAnthropic

    s = get_settings()
    client = AsyncAnthropic(base_url=s.litellm_url, api_key=s.litellm_master_key)
    msg = await client.messages.create(
        model=model, max_tokens=max_tokens, system=system or [], messages=[{"role": "user", "content": prompt}]
    )
    return "".join(b.text for b in msg.content if b.type == "text")


@dataclass
class AgentSession:
    """Everything one agent run needs; built by the sandbox runner from the role config."""

    prompt: str
    instructions: str  # appended to Claude Code's own system prompt
    model: str
    cwd: str
    allowed_tools: list[str]
    max_turns: int
    max_budget_usd: float
    output_schema: dict | None = None
    # PreToolUse checks: (tool_name, tool_input) -> deny reason, or None to allow.
    pre_tool_use: Callable[[str, dict], str | None] | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class AgentOutcome:
    ok: bool
    subtype: str  # success, error_max_turns, error_max_budget_usd, error_during_execution, ...
    structured: Any
    text: str | None
    turns: int
    cost_usd: float
    tokens_in: int
    tokens_out: int
    errors: list[str]
    denials: list[str] = field(default_factory=list)  # guard denials, reported to the supervisor


async def run_agent(session: AgentSession) -> AgentOutcome:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

    denials: list[str] = []
    hooks = None
    if session.pre_tool_use:
        check = session.pre_tool_use

        async def guard(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
            reason = check(input_data["tool_name"], input_data.get("tool_input") or {})
            if reason is None:
                return {}
            denials.append(f"{input_data['tool_name']}: {reason}")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        hooks = {"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]}

    options = ClaudeAgentOptions(
        # Claude Code's own agentic-coding prompt, plus the role prompt and project instructions.
        system_prompt={"type": "preset", "preset": "claude_code", "append": session.instructions},
        model=session.model,
        cwd=session.cwd,
        tools=session.allowed_tools,
        allowed_tools=session.allowed_tools,
        permission_mode="dontAsk",
        max_turns=session.max_turns,
        max_budget_usd=session.max_budget_usd,
        output_format={"type": "json_schema", "schema": session.output_schema} if session.output_schema else None,
        hooks=hooks,
        # Nothing from the repo's .claude/ or .mcp.json: project instructions arrive from the base
        # branch through the task, so the code under review cannot rewrite its reviewer.
        setting_sources=[],
        strict_mcp_config=True,
        env=session.env,
    )

    result: ResultMessage | None = None
    async for message in query(prompt=session.prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        return AgentOutcome(False, "no_result", None, None, 0, 0.0, 0, 0, ["agent produced no result"], denials)

    usage = result.usage or {}
    tokens_in = sum(
        usage.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return AgentOutcome(
        ok=not result.is_error and result.subtype == "success",
        subtype=result.subtype,
        structured=result.structured_output,
        text=result.result,
        turns=result.num_turns,
        cost_usd=result.total_cost_usd or 0.0,
        tokens_in=tokens_in,
        tokens_out=usage.get("output_tokens", 0) or 0,
        errors=list(result.errors or []),
        denials=denials,
    )


class LiteLLM:
    """Admin API of the model proxy: role teams with daily budgets, and per-run keys."""

    def __init__(self) -> None:
        s = get_settings()
        self.url = s.litellm_url
        self.headers = {"Authorization": f"Bearer {s.litellm_master_key}"}

    async def _post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=30) as c:
            r = await c.post(path, json=body)
            r.raise_for_status()
            return r.json()

    async def ensure_team(self, team_id: str | None, role: str, daily_budget_usd: float) -> str:
        """Create the role's team, or update its daily budget. Returns the team id."""
        if team_id:
            await self._post("/team/update", {"team_id": team_id, "max_budget": daily_budget_usd})
            return team_id
        data = await self._post(
            "/team/new", {"team_alias": f"agent-{role}", "max_budget": daily_budget_usd, "budget_duration": "1d"}
        )
        return data["team_id"]

    async def issue_key(self, run_id: str, team_id: str | None, max_budget_usd: float, hours: int = 2) -> str:
        body: dict[str, Any] = {
            "key_alias": run_id,
            "max_budget": max_budget_usd,
            "duration": f"{hours}h",
            "metadata": {"run_id": run_id},
        }
        if team_id:
            body["team_id"] = team_id
        return (await self._post("/key/generate", body))["key"]

    async def key_spend(self, key: str) -> float | None:
        async with httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=30) as c:
            r = await c.get("/key/info", params={"key": key})
            if r.status_code != 200:
                return None
            return float(r.json().get("info", {}).get("spend") or 0)

    async def revoke(self, key: str) -> None:
        await self._post("/key/delete", {"keys": [key]})
