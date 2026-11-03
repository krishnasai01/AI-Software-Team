#!/usr/bin/env bash
# Build the base and every stack image. Run from cron weekly on each worker server:
#   0 3 * * 1  cd /opt/agent-company && sandbox-images/build.sh
set -euo pipefail
cd "$(dirname "$0")/.."
docker build --pull -f sandbox-images/base/Dockerfile -t agent-company/sandbox-base:latest .
for dir in sandbox-images/*/; do
  stack=$(basename "$dir")
  [ "$stack" = base ] && continue
  docker build -t "agent-company/sandbox-$stack:latest" "$dir"
done
docker image prune -f >/dev/null
