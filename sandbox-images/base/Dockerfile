# Base for every sandbox: the agent runtime only. Product stacks build FROM this.
# Build from the repo root: docker build -f sandbox-images/base/Dockerfile -t agent-company/sandbox-base .
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The Python Agent SDK bundles the Claude Code CLI, so no Node is needed for the agent itself.
RUN pip install --no-cache-dir "claude-agent-sdk>=0.2.176" "httpx>=0.27" "pydantic-settings>=2.4" "pyyaml>=6.0"

# Runtime, prompts, configs and hooks are root-owned and read-only to the agent:
# agents cannot edit their own prompts, configs or guardrails.
COPY core /opt/agent-company/core
COPY agents /opt/agent-company/agents
RUN chmod -R a-w /opt/agent-company

RUN useradd --create-home --uid 10001 agent && mkdir /workspace && chown agent:agent /workspace
USER agent
ENV PYTHONPATH=/opt/agent-company \
    PYTHONDONTWRITEBYTECODE=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "agents.runner"]
