#!/usr/bin/env bash
# Restore both databases from a backup directory made by scripts/backup.sh.
# Usage: scripts/restore.sh backups/20261008T021500Z
# Stops n8n and the app while restoring. .env must hold the N8N_ENCRYPTION_KEY from
# that backup (see n8n-encryption-key.env in it).
set -euo pipefail
cd "$(dirname "$0")/.."
dir=${1:?usage: scripts/restore.sh <backup-dir>}
saved_key=$(cut -d= -f2- "$dir/n8n-encryption-key.env")
current_key=$(grep '^N8N_ENCRYPTION_KEY=' .env | cut -d= -f2-)
if [[ "$saved_key" != "$current_key" ]]; then
  echo "N8N_ENCRYPTION_KEY in .env differs from the backup. Put the backup's key in .env first." >&2
  exit 1
fi
docker compose stop app n8n n8n-worker
for db in inbox n8n; do
  docker compose exec -T postgres pg_restore -U postgres -d "$db" --clean --if-exists --no-owner --role="$db" < "$dir/$db.dump"
done
docker compose start app n8n n8n-worker
echo "Restored from $dir."
