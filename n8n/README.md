# n8n workflows

Exported workflow JSON lives in `workflows/`. Credentials are referenced by name and never committed.

| File | Workflow | Status |
|---|---|---|
| `01_intake.json` | WF1 Intake: Gmail → Django `/internal/emails` → label → start WF2 | Built (M1) |
| `02_process.json` | WF2 Process: triage → route → HubSpot / ShipMatch / Slack → draft → approval | M3–M4 |
| `03_execute.json` | WF3 Execute: approval → reply in thread → labels | M4 |
| `04_error.json` | WF4 Error handler | M5 |
| `05_digest.json` | WF5 Daily digest | M5 |

## Import

The compose file mounts this folder read-only at `/workflows` in the n8n container:

```bash
docker compose exec n8n n8n import:workflow --input=/workflows/01_intake.json
```

Or use the editor: **Workflows → Import from file**.

## Credentials to create in n8n

| Name (exact) | Type | Used by |
|---|---|---|
| `Gmail ops inbox` | Gmail OAuth2 | WF1 trigger, "Get full message", labels, WF3 reply |
| `Slack` | Slack OAuth2 / bot token | WF2, WF4, WF5 (later) |
| `HubSpot` | HubSpot App Token (private app) | WF2 (later) |

Gmail OAuth: create an OAuth client (type *Web application*) in a Google Cloud project, add the n8n
redirect URL shown in the credential dialog, and enable the Gmail API. Scopes needed: read, modify
(labels) and send.

## After importing WF1

1. In Gmail, create the labels `ai-processing`, `ai-processed`, `ai-rejected`, `ai-skip`.
2. Open **Label ai-processing** and pick the `ai-processing` label (the export holds a placeholder ID).
3. Open **Start WF2 Process** and select WF2 once it exists.
4. Workflow settings → Error workflow → WF4 (once it exists).
5. Activate the workflow.

## Environment the workflows read

Set in `docker-compose.yml` from `.env` (`N8N_BLOCK_ENV_ACCESS_IN_NODE=false` so `{{ $env.X }}` works):

| Variable | Purpose |
|---|---|
| `APP_URL` | Django inside the Docker network (`http://app:8001`) |
| `INTERNAL_TOKEN` | Sent as `X-Internal-Token` on every call to `/internal/` |
| `DASHBOARD_BASE_URL` | Links to the dashboard in Slack messages |
| `SHIPMATCH_*` | ShipMatch integration (M3) |

## How WF1 prevents duplicates

1. The Gmail search skips anything already labelled `ai-processing`, `ai-processed`, `ai-skip` or `ai-rejected`.
2. Django inserts on the unique `gmail_message_id` and returns `created: false` for a repeat; the IF node
   then stops. Both checks together mean a re-run of the trigger creates no new rows.

The message is fetched with the Gmail API (`format=full`) through the Gmail credential, and the
**Build payload** Code node extracts headers, the text/HTML bodies and attachment metadata (attachments
are not downloaded at intake).
