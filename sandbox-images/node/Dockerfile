# Stack image for Node products, with Playwright's browsers for QA in Phase 2.
ARG BASE=agent-company/sandbox-base:latest
FROM ${BASE}
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g pnpm
USER agent
