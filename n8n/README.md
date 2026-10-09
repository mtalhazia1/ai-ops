# n8n workflows

Exported workflow JSON lives in `workflows/`. Credentials are referenced by name and never committed.

| File | Workflow | Status |
|---|---|---|
| `01_intake.json` | WF1 Intake: Gmail → Django `/internal/emails` → label → start WF2 | Built (M1) |
| `02_process.json` | WF2 Process: triage → route → HubSpot / ShipMatch / Slack → draft → Slack approval | Built (M3, M4) |
| `03_execute.json` | WF3 Execute: record decision → reply in thread → labels | Built (M4) |
| `04_error.json` | WF4 Error handler: record failure in Django → Slack `#ops-alerts` | Built (M5) |
| `05_digest.json` | WF5 Daily digest: 18:00 metrics summary → Slack | Built (M5) |
| `../template/gmail-claude-slack-approval.json` | Standalone 11-node template for n8n's library (Section 18) | Built; see `template/README.md` |

## Import

Quickest: `scripts/import_workflows.sh` (add `docker-compose.prod.yml` or `docker-compose.mocks.yml` as an
argument when you use them). By hand: the compose file mounts this folder read-only at `/workflows` in the n8n
container:

```bash
docker compose exec n8n n8n import:workflow --input=/workflows/01_intake.json
docker compose exec n8n n8n import:workflow --input=/workflows/02_process.json
docker compose exec n8n n8n import:workflow --input=/workflows/03_execute.json
docker compose exec n8n n8n import:workflow --input=/workflows/04_error.json
docker compose exec n8n n8n import:workflow --input=/workflows/05_digest.json
docker compose exec n8n n8n publish:workflow --id=wf2process000001   # activates WF2's retry webhook
docker compose exec n8n n8n publish:workflow --id=wf3execute000001   # activates WF3's dashboard webhook
docker compose exec n8n n8n publish:workflow --id=wf4error00000001   # error workflows only run when published
docker compose exec n8n n8n publish:workflow --id=wf5digest0000001   # activates the 18:00 schedule
docker compose restart n8n n8n-worker                                 # CLI publish takes effect on restart
```

Or use the editor: **Workflows → Import from file**. Workflow IDs are fixed (`wf1intake0000001`,
`wf2process000001`, `wf3execute000001`, `wf4error00000001`, `wf5digest0000001`), so the workflows already
point at each other after import: WF1 → WF2 → WF3, and WF1–WF3 and WF5 use WF4 as their error workflow.
WF4 must be published too: n8n only runs a published error workflow. `scripts/import_workflows.sh` does all
of this in one go.

## Credentials to create in n8n

| Name (exact) | Type | Used by |
|---|---|---|
| `Gmail ops inbox` | Gmail OAuth2 | WF1 trigger, "Get full message", labels; WF2 attachment download; WF3 reply |
| `Slack` | Slack API (bot token `xoxb-...`, scope `chat:write`) | WF2 review/claim alerts; WF4, WF5 later |
| `HubSpot` | HubSpot App Token (private app) | WF2 contacts, deals, notes, tasks |
| `ShipMatch API` | Header Auth: name `Authorization`, value `Bearer sm_...` | WF2 lookups and uploads |

After importing, open each HTTP/Gmail node that shows a credential warning and pick the credential
(n8n links credentials by ID, and IDs differ per install).

Gmail OAuth: create an OAuth client (type *Web application*) in a Google Cloud project, add the n8n
redirect URL shown in the credential dialog, and enable the Gmail API. Scopes needed: read, modify
(labels) and send.

## After importing

1. In Gmail, create the labels `ai-processing`, `ai-processed`, `ai-rejected`, `ai-skip`.
2. Put their **IDs** (not names) in `.env` as `GMAIL_LABEL_PROCESSING_ID`, `GMAIL_LABEL_PROCESSED_ID`,
   `GMAIL_LABEL_REJECTED_ID`. To list them, run a one-off HTTP Request node with the Gmail credential:
   `GET https://gmail.googleapis.com/gmail/v1/users/me/labels` (IDs look like `Label_123456789`).
3. Publish WF1 (the Gmail trigger starts polling), WF2 and WF3.

## Environment the workflows read

Set in `docker-compose.yml` from `.env` (`N8N_BLOCK_ENV_ACCESS_IN_NODE=false` so `{{ $env.X }}` works):

| Variable | Purpose |
|---|---|
| `APP_URL` | Django inside the Docker network (`http://app:8001`) |
| `INTERNAL_TOKEN` | Sent as `X-Internal-Token` on every call to `/internal/` |
| `DASHBOARD_BASE_URL` | Links to the dashboard in Slack messages |
| `N8N_WEBHOOK_SECRET` | Must match the `x-webhook-secret` header on `POST /webhook/process` (WF2 Retry) |
| `SHIPMATCH_URL`, `SHIPMATCH_ORG` | ShipMatch API (Django's `SHIPMATCH_ENABLED` decides whether WF2 uses it) |
| `HUBSPOT_QUOTES_PIPELINE_ID`, `HUBSPOT_STAGE_NEW_REQUEST`, `HUBSPOT_STAGE_INFO_REQUESTED` | Deal pipeline and stages ("Quotes": New request / Info requested) |
| `HUBSPOT_OPS_OWNER_ID` | Owner of booking tasks (optional) |
| `SLACK_CHANNEL_REVIEW`, `_URGENT`, `_APPROVALS`, `_ALERTS`, `_DIGEST` | Channel names; invite the bot to each |
| `N8N_EDITOR_BASE_URL` | Used in WF4 alerts to link to the failed execution |
| `GMAIL_LABEL_*_ID` | Gmail label IDs for ai-processing / ai-processed / ai-rejected |
| `APPROVAL_TIMEOUT_MINUTES` | How long an approval card waits before the email goes back to review (default 1440 = 24 h) |
| `HUBSPOT_API_URL`, `SLACK_API_URL`, `GMAIL_API_URL` | API base URLs; leave unset in production (only the mocks override them) |

## How WF1 prevents duplicates

1. The Gmail search skips anything already labelled `ai-processing`, `ai-processed`, `ai-skip` or `ai-rejected`.
2. Django inserts on the unique `gmail_message_id` and returns `created: false` for a repeat; the IF node
   then stops. Both checks together mean a re-run of the trigger creates no new rows.

The message is fetched with the Gmail API (`format=full`) through the Gmail credential, and the
**Build payload** Code node extracts headers, the text/HTML bodies and attachment metadata (attachments
are not downloaded at intake).

## WF2 Process

```
WF1 / Retry webhook ─► Email ID ─► Triage (Django) ─► Needs review? ── yes ─► claim ─► Slack #ops-review
                                                            │ no
                                                            ▼
                                                        Category
   quote_request ─┬► HubSpot upsert contact ─► Quote? ─ yes ─► claim deal ─► create deal ─► claim note ─► add note
   booking ───────┘                                    └ no ─► claim task ─► create task
   shipment_status ─► ShipMatch on and refs? ─► one item per ref ─► ShipMatch search ─► summarize ─► log lookup
   paperwork ─► ShipMatch on and attachments? ─ yes ─► per attachment: claim ─► Gmail get attachment ─► file ─► ShipMatch upload ─► log
                                              └ no ─► status needs_review ─► Slack #ops-review
   claim ─► claim alert ─► Slack #ops-urgent

 (each branch end) ─► Draft (Django) ─ ok ─► claim ─► Slack approval card ─► Wait for decision
                                   └ blocked ─► Slack #ops-review              ├ approve ─► WF3 (approved)
                                                                               ├ reject  ─► WF3 (rejected)
                                                                               └ timeout ─► needs_review ─► Slack #ops-review
```

- **Triage** is one HTTP call: Django cleans, classifies, extracts and validates, and returns everything
  the branches need (`crm.*`, `slack.*`, `lookup_refs`, `upload_attachments`, `shipmatch_enabled`).
  The same call on an email already triaged returns the saved result (`rerun: true`) without another LLM call.
- **Exactly once**: before every outside create, `POST /internal/emails/{id}/actions/claim` reserves the
  idempotency key. Only the run that gets `proceed: true` makes the call, then logs it with
  `POST /internal/emails/{id}/actions`. A deal that already exists is reused for the note. The HubSpot
  contact is a batch *upsert* keyed on email, so it can't duplicate.
- **Identity values come from code**: the contact's email and name come from the Gmail header; the
  LLM's extracted fields only go into the deal name and note text.
- **Retries**: every external HTTP node retries twice with a 5 s wait. Slack replies `200 {ok:false}` on
  errors, so an IF checks `ok` and a *Stop and Error* node fails the run (WF4 then records it and frees the
  claims). ShipMatch uploads never throw: the status code is logged (`ok=false` for 400/402).
- **Retry webhook**: `POST /webhook/process` with header `x-webhook-secret: $N8N_WEBHOOK_SECRET` and body
  `{"email_id": 123}`. The dashboard's Retry button (M5) will call it after `failed → received`.
- ShipMatch off (`SHIPMATCH_ENABLED=false` in `.env`): status emails skip the lookup; paperwork emails go to
  `needs_review` with a Slack review message.
- **Drafting**: Django drafts from the structured triage data plus the playbook (never the raw email), runs
  the output checks (no new addresses or links, no money amounts, length) and moves the email to
  `awaiting_approval`, or to `needs_review` if the draft is blocked. A repeated call returns the saved draft.
- **Approval**: one card per email in `#ops-approvals` showing category, sender, fields, what was already
  done and the draft, with **Approve and send**, **Reject** and **Edit in dashboard** buttons. The first two
  are signed links to the Wait node's resume URL; the Wait node ignores bots (link previewers can't approve)
  and n8n refuses a second click (409). With no decision before `APPROVAL_TIMEOUT_MINUTES`, the email goes
  back to review. Slack links don't identify who clicked, so Slack decisions are recorded as reviewer `slack`.

## WF3 Execute

Started by WF2 (Slack decision) or by `POST /webhook/execute` (dashboard, header `x-webhook-secret`, body
`{"email_id", "decision": "approved"|"rejected", "reviewer", "channel": "dashboard", "final_body"}`).

1. **Record decision**: `POST /internal/emails/{id}/approval`. Django checks the state, saves the `Approval`,
   moves the email to `approved`/`rejected` and returns the reply as a raw RFC 5322 message. The recipient
   (Reply-To or From) and the `In-Reply-To`/`References` headers come from the stored Gmail metadata. The same
   decision repeated returns the saved one, so a retried WF3 can finish; a different one is refused (409),
   and WF3 stops quietly.
2. **Rejected**: label `ai-rejected`, remove `ai-processing`.
3. **Approved**: claim `reply_sent:{id}` → Gmail `messages.send` with `threadId` (no automatic retry on
   this node; the claim makes a manual retry safe) → log → status `done` → label `ai-processed`.
   If an earlier run sent the reply but crashed before finishing, the claim says *done* and WF3 just
   finishes the status and labels.
4. **Send failure**: WF4 (M5) records the failure, which frees the claim; Retry calls WF3 again with the same
   decision and Django resumes at sending (`failed → approved`) without a new draft or approval.

## WF4 Error handler

Every workflow names WF4 as its error workflow. On any failed execution (an HTTP node out of retries,
a *Stop and Error* node, a trigger failure):

1. **Failure details** (Code): workflow, node, error message, execution ID and URL from the Error Trigger.
2. **Record failure** → `POST /internal/failures`. n8n's error payload doesn't say which email failed, so
   every internal call from WF1–WF3 sends the header `X-N8N-Execution-Id`, and Django remembers the last
   execution per email. Django finds the email, saves a `Failure`, moves the email to `failed`, frees the
   claims the run held, and returns the alert text.
3. **Slack `#ops-alerts`**: "⚠️ WF2 Process failed at HubSpot upsert contact: …" with links to the email
   in the dashboard and to the execution in n8n. If Django itself is down, a plain alert is still posted.

**Retry** (dashboard → Failures, or the email page): if a reply was already approved, it calls WF3 again
and resumes at sending; otherwise it sets the email back to `received` and calls WF2's webhook. WF2
reuses the saved triage (and any corrections made in the dashboard), so a retry costs no new
classification, and the claims stop duplicate HubSpot records, uploads or alerts.

## WF5 Daily digest

Schedule trigger at 18:00 (workflow timezone Asia/Karachi) → `GET /internal/metrics?period=today` →
Slack `SLACK_CHANNEL_DIGEST`: emails handled by category, replies sent (fully automatic vs reviewed or
edited), rejected, average time to reply, AI cost, injection attempts flagged, failures, and what is
waiting now. **Run now** (manual trigger) sends it on demand:
`docker compose exec n8n n8n execute --id=wf5digest0000001`.

## Running offline with mocks

`docker-compose.mocks.yml` adds a `mocks` service (`dev/mocks/mock_services.py`) that stands in for
HubSpot, Slack, ShipMatch, Gmail attachments and the Anthropic API (a few regexes, not a real model).

```bash
docker compose -f docker-compose.yml -f docker-compose.mocks.yml up -d
docker compose cp dev/mocks/n8n-credentials.json n8n:/tmp/creds.json
docker compose exec n8n n8n import:credentials --input=/tmp/creds.json   # fake tokens only
# import + publish WF2 as above, restart n8n, then:
python dev/e2e_wf2.py --runs 3
python dev/e2e_wf2.py quote --approve     # clicks the Slack card's Approve button like a browser
```

For a quick timeout test set `APPROVAL_TIMEOUT_MINUTES=1` in `.env`. To test WF4 and Retry, import the
credentials with the HubSpot token changed to `pat-bad` (the mock answers 401), send a quote email, then
re-import the good credentials and press Retry in the dashboard.

Never import the mock credentials into a real deployment.
