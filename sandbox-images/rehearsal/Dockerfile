# A scripted agent for $0 end-to-end rehearsals: same runner plumbing, no model calls.
# docker build -t agent-company/sandbox-rehearsal sandbox-images/rehearsal
ARG BASE=agent-company/sandbox-base:latest
FROM ${BASE}
ENTRYPOINT ["python", "-m", "agents.rehearsal"]
