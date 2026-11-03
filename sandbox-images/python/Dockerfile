# Stack image for Python products. Rebuild weekly (see sandbox-images/build.sh).
ARG BASE=agent-company/sandbox-base:latest
FROM ${BASE}
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv pytest ruff
USER agent
