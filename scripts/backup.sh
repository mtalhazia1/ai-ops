#!/usr/bin/env bash
# Back up both databases (inbox + n8n) and the n8n encryption key.
# Usage: scripts/backup.sh [backup-dir]   (default: ./backups). Keeps the newest 14.
# Copy the result off the server (e.g. rclone/restic to object storage) from cron:
#   15 2 * * * cd /opt/ai-ops && scripts/backup.sh && rclone copy backups remote:ai-ops-backups
set -euo pipefail
cd "$(dirname "$0")/.."
root=${1:-backups}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dir="$root/$stamp"
mkdir -p "$dir"
chmod 700 "$root" "$dir"
for db in inbox n8n; do
  docker compose exec -T postgres pg_dump -U postgres -Fc "$db" > "$dir/$db.dump"
done
# Without this key the n8n dump's credentials can't be decrypted.
grep '^N8N_ENCRYPTION_KEY=' .env > "$dir/n8n-encryption-key.env"
chmod 600 "$dir"/*
ls -1dt "$root"/*/ | tail -n +15 | xargs -r rm -rf
echo "Backup written to $dir:"
ls -lh "$dir"
