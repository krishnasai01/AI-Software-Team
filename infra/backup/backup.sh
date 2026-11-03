#!/usr/bin/env bash
# Daily encrypted Postgres backup, shipped off the server. Run on the control server:
#   15 2 * * *  /opt/agent-company/infra/backup/backup.sh || <alert>
# Needs: BACKUP_AGE_RECIPIENT (age public key; the private key is kept OFF the servers),
#        BACKUP_REMOTE (an rclone remote, e.g. "b2:agent-company-backups").
set -euo pipefail
: "${BACKUP_AGE_RECIPIENT:?}" "${BACKUP_REMOTE:?}"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
file="/var/backups/agent-company/pg-${stamp}.sql.gz.age"
mkdir -p "$(dirname "$file")"

docker compose -p agent-company-control exec -T postgres pg_dumpall -U agent \
  | gzip -9 | age -r "$BACKUP_AGE_RECIPIENT" > "$file"

rclone copy "$file" "$BACKUP_REMOTE/"
rclone delete --min-age 30d "$BACKUP_REMOTE/"
find "$(dirname "$file")" -name 'pg-*.age' -mtime +3 -delete
echo "backup ok: $(basename "$file") ($(du -h "$file" | cut -f1))"
