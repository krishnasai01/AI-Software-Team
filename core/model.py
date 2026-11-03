"""Model access. The only place the Anthropic SDKs are imported.

- `ask_model()`: a single completion for supervisor summaries and digests (control side).
- `run_agent()`: one Claude Agent SDK session. It runs *inside* a sandbox, called by the runner.
- `RunKeys`: mints a LiteLLM virtual key per run, so the real Anthropic key never enters a
  sandbox and each run has a hard budget enforced by the proxy, not just by the agent.
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
        model=model,
        max_tokens=max_tokens,
        system=system or [],
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


@dataclass
class AgentSession:
    """Everything one agent run needs; built by the sandbox runner from the role config."""

    prompt: str
    system_prompt: str
    model: str
    cwd: str
    allowed_tools: list[str]
    max_turns: int
    max_budget_usd: float
    output_schema: dict | None = None
    # PreToolUse checks: (tool_name, tool_input) -> deny reason, or None to allow.
    pre_tool_use: Callable[[str, dict], str | None] | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
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
    session_id: str | None
    errors: list[str]


async def run_agent(session: AgentSession) -> AgentOutcome:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

    hooks = None
    if session.pre_tool_use:
        check = session.pre_tool_use

        async def guard(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
            reason = check(input_data["tool_name"], input_data.get("tool_input") or {})
            if reason is None:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        hooks = {"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]}

    options = ClaudeAgentOptions(
        system_prompt=session.system_prompt,
        model=session.model,
        cwd=session.cwd,
        tools=session.allowed_tools,
        allowed_tools=session.allowed_tools,
        permission_mode="dontAsk",
        max_turns=session.max_turns,
        max_budget_usd=session.max_budget_usd,
        output_format={"type": "json_schema", "schema": session.output_schema} if session.output_schema else None,
        hooks=hooks,
        mcp_servers=session.mcp_servers,
        # Only the repo's own CLAUDE.md / .claude settings; nothing from the image's home dir.
        setting_sources=["project"],
        env=session.env,
    )

    result: ResultMessage | None = None
    async for message in query(prompt=session.prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        return AgentOutcome(False, "no_result", None, None, 0, 0.0, 0, 0, None, ["agent produced no result"])

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
        session_id=result.session_id,
        errors=list(result.errors or []),
    )


class RunKeys:
    """Per-run LiteLLM virtual keys."""

    def __init__(self) -> None:
        s = get_settings()
        self.url = s.litellm_url
        self.headers = {"Authorization": f"Bearer {s.litellm_master_key}"}

    async def issue(self, run_id: str, models: list[str], max_budget_usd: float, hours: int = 2) -> str:
        async with httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=30) as c:
            r = await c.post(
                "/key/generate",
                json={
                    "key_alias": run_id,
                    "models": models,
                    "max_budget": max_budget_usd,
                    "duration": f"{hours}h",
                    "metadata": {"run_id": run_id},
                },
            )
            r.raise_for_status()
            return r.json()["key"]

    async def spend(self, key: str) -> float | None:
        async with httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=30) as c:
            r = await c.get("/key/info", params={"key": key})
            if r.status_code != 200:
                return None
            return float(r.json().get("info", {}).get("spend") or 0)

    async def revoke(self, key: str) -> None:
        async with httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=30) as c:
            await c.post("/key/delete", json={"keys": [key]})
