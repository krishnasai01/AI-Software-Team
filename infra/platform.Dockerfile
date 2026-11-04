# Image for the platform processes: webhook receiver, workers, migrations.
# Build from the repo root: docker build -f infra/platform.Dockerfile -t agent-company/platform .
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates docker.io \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY core core
COPY agents agents
COPY orchestrator orchestrator
COPY db db
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
