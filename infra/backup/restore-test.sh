#!/usr/bin/env bash
# Monthly restore drill: restore the newest backup into a throwaway Postgres and sanity-check it.
# Run on an admin machine that holds the age private key (never on the servers):
#   AGE_IDENTITY=~/.age/agent-company.key BACKUP_REMOTE=b2:agent-company-backups ./restore-test.sh
set -euo pipefail
: "${AGE_IDENTITY:?}" "${BACKUP_REMOTE:?}"
tmp=$(mktemp -d); trap 'docker rm -f restore-drill >/dev/null 2>&1; rm -rf "$tmp"' EXIT
latest=$(rclone lsf "$BACKUP_REMOTE/" --include 'pg-*.age' | sort | tail -1)
rclone copy "$BACKUP_REMOTE/$latest" "$tmp/"

docker run -d --name restore-drill -e POSTGRES_PASSWORD=drill pgvector/pgvector:pg16 >/dev/null
until docker exec restore-drill pg_isready -q; do sleep 1; done
age -d -i "$AGE_IDENTITY" "$tmp/$latest" | gunzip | docker exec -i restore-drill psql -q -U postgres >/dev/null

for t in products agents claims runs spend_daily approvals events_inbox; do
  n=$(docker exec restore-drill psql -U postgres -d agent_company -tAc "SELECT count(*) FROM $t")
  echo "$t: $n rows"
done
echo "restore drill ok: $latest"
