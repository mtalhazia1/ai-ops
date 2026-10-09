#!/usr/bin/env bash
# Create .env from .env.example with fresh random secrets. Never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  echo ".env already exists; leaving it alone." >&2
  exit 1
fi
rand() { openssl rand -hex "$1"; }
cp .env.example .env
set_var() {  # set_var NAME VALUE: replace the line NAME=... (keeps comments elsewhere)
  local name=$1 value=$2
  sed -i.bak -E "s|^${name}=.*|${name}=${value}|" .env && rm -f .env.bak
}
set_var DJANGO_SECRET_KEY "$(rand 32)"
set_var DJANGO_DEBUG false
set_var INTERNAL_TOKEN "$(rand 32)"
set_var N8N_WEBHOOK_SECRET "$(rand 24)"
set_var N8N_ENCRYPTION_KEY "$(rand 32)"
set_var POSTGRES_PASSWORD "$(rand 16)"
set_var DB_POSTGRESDB_PASSWORD "$(rand 16)"
pw=$(rand 16)
set_var INBOX_DB_PASSWORD "$pw"
set_var DATABASE_URL "postgres://inbox:${pw}@postgres:5432/inbox"
chmod 600 .env
echo "Created .env with new secrets. Next: add ANTHROPIC_API_KEY (and the other blanks you need)."
echo "Back up N8N_ENCRYPTION_KEY somewhere safe: without it n8n can't read its stored credentials."
