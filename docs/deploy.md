# Deploying AI Ops Inbox

One small VPS (2 vCPU / 4 GB, Ubuntu 24.04, e.g. Hetzner CX22 or a DigitalOcean 4 GB droplet) runs
everything: Postgres, Redis, n8n main + worker, Django, and Caddy for HTTPS.

## 1. Server

```bash
# as root on a fresh server
apt-get update && apt-get install -y docker.io docker-compose-v2 git ufw
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
git clone https://github.com/<you>/ai-ops.git /opt/ai-ops && cd /opt/ai-ops
```

DNS: two A records pointing at the server, e.g. `inbox.example.com` (dashboard) and `n8n.example.com`
(n8n editor and the signed approval links in Slack).

## 2. Configuration

```bash
scripts/init_env.sh          # .env with fresh random secrets (never overwrites an existing one)
nano .env                    # fill in the values below
```

| Variable | Value |
|---|---|
| `APP_DOMAIN`, `N8N_DOMAIN`, `ACME_EMAIL` | The two hostnames and the Let's Encrypt contact |
| `ANTHROPIC_API_KEY` | Your API key |
| `SHIPMATCH_ENABLED`, `SHIPMATCH_URL`, `SHIPMATCH_ORG` | If ShipMatch is used (API key goes in n8n) |
| `HUBSPOT_QUOTES_PIPELINE_ID`, `HUBSPOT_STAGE_*`, `HUBSPOT_OPS_OWNER_ID` | From HubSpot: Settings → Objects → Deals → Pipelines |
| `SLACK_CHANNEL_*` | Channel names; invite the bot to each |
| `GMAIL_LABEL_*_ID` | Filled in after step 4 |

**Copy `N8N_ENCRYPTION_KEY` to your password manager now.** Without it, a restored n8n can't decrypt its
stored credentials.

## 3. Start

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose exec app python manage.py createsuperuser
scripts/import_workflows.sh docker-compose.prod.yml
```

Caddy obtains certificates for both domains on first start. Only ports 80/443 are published: the
`/internal` API and the WF2/WF3 webhooks are reachable only on the Docker network (Caddy answers 404 for them
from outside).

Check: `https://APP_DOMAIN/` shows the login; `https://N8N_DOMAIN/` shows n8n's owner setup (create the n8n
owner account straight away).

## 4. Connect the accounts (in n8n)

Create these credentials, with exactly these names (details in `n8n/README.md`):

| Name | Type |
|---|---|
| `Gmail ops inbox` | Gmail OAuth2 (scopes: read, modify, send) |
| `Slack` | Slack API, bot token with `chat:write` |
| `HubSpot` | HubSpot App Token (private app: contacts, deals, tasks/notes write) |
| `ShipMatch API` | Header Auth: `Authorization` = `Bearer sm_...` |

Open each workflow, pick the credential in any node that shows a warning, and save. In Gmail, create the labels
`ai-processing`, `ai-processed`, `ai-rejected`, `ai-skip`; put their IDs in `.env`; then
`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` and publish **WF1** in the editor.

## 5. Backups

```bash
crontab -e
# daily at 02:15: both databases + the encryption key, newest 14 kept, then off the server
15 2 * * * cd /opt/ai-ops && scripts/backup.sh >> /var/log/ai-ops-backup.log 2>&1 && rclone copy backups remote:ai-ops-backups
# daily body retention (default 90 days; metrics and audit rows are kept)
30 2 * * * cd /opt/ai-ops && docker compose exec -T app python manage.py purge_old_bodies >> /var/log/ai-ops-purge.log 2>&1
```

Restore (same or new server, with the backup's `N8N_ENCRYPTION_KEY` in `.env`):

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml scripts/restore.sh backups/20261008T021500Z
```

## 6. Updating

```bash
cd /opt/ai-ops && git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build   # migrations run on start
scripts/import_workflows.sh docker-compose.prod.yml                             # if workflows changed
```

Re-importing a workflow replaces it; re-attach credentials if n8n shows a warning, and re-publish WF1.

## Cost

VPS about $10–20/month. AI cost per email is reported by `run_eval` and on the Metrics page.
