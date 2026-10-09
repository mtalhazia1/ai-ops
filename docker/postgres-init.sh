#!/bin/bash
# Creates the two application databases (n8n and inbox) with their own users.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-SQL
  CREATE USER n8n WITH PASSWORD '${DB_POSTGRESDB_PASSWORD}';
  CREATE DATABASE n8n OWNER n8n;
  CREATE USER inbox WITH PASSWORD '${INBOX_DB_PASSWORD}';
  CREATE DATABASE inbox OWNER inbox;
SQL
