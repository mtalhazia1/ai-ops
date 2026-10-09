# AI Ops Inbox

**AI that reads a company's shared inbox, works out what each email needs, puts the data into the right
systems, drafts the reply, and asks a person to approve before anything is sent.**

Built with **n8n** (orchestration) + **Django** (AI logic, state, dashboard) + **Claude** (classification,
extraction, drafting). The demo company is *Indus Freight*, a made-up freight brokerage; switching industry
means changing the categories, fields and playbook, not the code.

![Email workspace](docs/screenshots/email-workspace.png)

## What it does

| Email | What happens |
|---|---|
| "Need a rate for 4 pallets Lahore→Karachi, pickup Tue" | HubSpot contact + deal + note; reply asks for anything missing |
| "Confirming booking for quote Q-1042" | HubSpot task for ops; booking confirmation drafted |
| "Where is container MSCU1234566?" | Looked up in ShipMatch; status reply drafted |
| Invoice or bill of lading attached | Attachments uploaded to ShipMatch; acknowledgement drafted |
| "Cargo arrived damaged, 2 cartons crushed" | Slack `#ops-urgent` at once; holding reply drafted |
| Newsletter, out-of-office, unclear, **prompt injection** | Human review queue, no draft |

Every reply goes to Slack as an approval card (**Approve and send / Reject / Edit in dashboard**), and is sent
only after a person decides, in the original Gmail thread.

## Architecture

![Architecture](docs/architecture.png)

| Concern | Lives in | Why |
|---|---|---|
| Gmail, Slack, HubSpot, ShipMatch, schedules, retries, waiting for approval | **n8n** (5 workflows, queue mode) | n8n's strength; visible, editable flows |
| Prompts, schemas, validation, injection checks, output checks | **Django** `/internal` API | One copy of the AI logic that production and the evaluation both call |
| Email state machine, audit trail, idempotency | **Postgres** via Django | One source of truth; invalid moves return 409 |
| Review queue, editing, failures, metrics, playbook | **Django** templates + HTMX | n8n has no end-user UI |

## What makes it production-grade

- **Nothing is sent without a person.** Slack approval or the dashboard; unanswered drafts go back to review
  after 24 h. Slack link-preview bots can't approve (the resume link ignores bots); a second click is refused.
- **Exactly once.** Every outside action (HubSpot record, upload, Slack alert, sent reply) is *claimed* in
  Postgres before the call, so retries and parallel runs never duplicate it. The email row is locked during
  AI triage, so a parallel run reuses the result instead of paying twice.
- **Prompt-injection safety.** The AI only classifies, extracts and drafts; every action is a fixed branch chosen
  by category. Recipients and threading come from Gmail metadata, never from AI output. The drafter never sees
  the raw email. Injection is detected twice (classifier flag + keyword check) and routes to review.
- **No invented values.** Every extracted field needs a quote from the email; values without one are dropped.
  Drafts with new addresses, links or money amounts are blocked by plain code.
- **Failures are visible and recoverable.** An error workflow records each failure against its email and alerts
  Slack; **Retry** resumes where it stopped (at sending if a reply was already approved).
- **Measured quality.** `run_eval` runs the production code over 62 hand-labelled emails and reports accuracy,
  invented values, injection catch rate, draft checks, latency and cost.

## Results

### Evaluation (62 labelled emails)

| Metric | Target | Result |
|---|---|---|
| Category accuracy | ≥ 90% | *pending first run* |
| Category macro F1 | ≥ 0.88 | *pending* |
| Field accuracy | ≥ 90% | *pending* |
| No invented values | 100% | *pending* |
| Injection emails routed to review | 100% | *pending* |
| Drafts passing output checks | ≥ 95% | *pending* |
| Latency p50 / p95, cost per email | report | *pending* |

> The numbers above are filled in from the committed report in `app/evals/reports/` after the first run with
> an Anthropic API key (`docker compose exec app python manage.py run_eval`). Only real report numbers go here.

The dataset covers 10+ emails per category and every hard case: forwarded and reply chains, Roman Urdu and
typos, missing fields, two requests in one email, an invoice attached to a status question, out-of-office and
newsletters, 5 prompt-injection emails, and invalid container numbers (ISO 6346 check digit).

### Reliability (measured end to end against the offline mocks)

| Scenario | Result |
|---|---|
| 7 emails × 3 overlapping WF2 runs | 1 HubSpot contact/deal/note/task, 1 upload set, 1 alert each; LLM calls at the minimum (13) |
| Approve clicked twice; a link-preview bot "clicks" first | Bot refused (403), second click refused (409), exactly one reply sent |
| Draft with an invented link | Blocked, email to review with the reason, Slack review alert |
| No decision before the timeout | Back to review with an alert; a late click is refused |
| HubSpot token revoked | Failure row + Slack alert naming the email; Retry after the fix completes with no duplicates |
| Gmail send fails after approval | Retry resumes at sending; no second draft or approval |
| Fresh `git clone` + `scripts/init_env.sh` + `docker compose up` + `scripts/import_workflows.sh` | Demo reproduced; with the production overlay, HTTPS via Caddy, `/internal` and private webhooks blocked at the edge |
| `scripts/backup.sh` → wipe → `scripts/restore.sh` | Both databases and n8n credentials restored |

Plus 150+ unit and API tests (LLM mocked), set up to run in GitHub Actions (`.github/workflows/tests.yml`).

## Screenshots

*Taken in offline demo mode (`docker-compose.mocks.yml`): a rule-based stand-in replaces Claude and the outside
services, so the triage text shown is not model output.*

| | |
|---|---|
| ![Review queue](docs/screenshots/review-queue.png) Review queue | ![Reply checks](docs/screenshots/reply-checks.png) Output checks on an edited reply |
| ![Injection flagged](docs/screenshots/injection-flagged.png) Injection attempt held for review | ![Timeline](docs/screenshots/email-done-timeline.png) Audit timeline of a sent reply |
| ![Metrics](docs/screenshots/metrics.png) Metrics | ![Failures](docs/screenshots/failures.png) Failures with Retry |

## Quick start (local)

```bash
scripts/init_env.sh                 # .env with fresh secrets; then add ANTHROPIC_API_KEY
docker compose up -d --build
docker compose exec app python manage.py createsuperuser
scripts/import_workflows.sh         # imports and publishes the n8n workflows
```

| URL | What |
|---|---|
| http://localhost:8001/ | Review queue (emails waiting for a person) |
| http://localhost:8001/emails/ | All emails; each opens a workspace: timeline, details editor, reply editor |
| http://localhost:8001/failures/ | Failed executions with Retry |
| http://localhost:8001/metrics/ | Handled, automatic vs reviewed, time to reply, AI cost, injections, failures |
| http://localhost:8001/playbook/ | Company facts, tone and signature used for drafts |
| http://localhost:5678/ | n8n editor |

Then connect the accounts in n8n (Gmail OAuth, Slack bot, HubSpot private app, ShipMatch key) and set the Gmail
label IDs: see [`n8n/README.md`](n8n/README.md).

**Try it with no accounts at all:** the offline mocks stand in for Gmail, Slack, HubSpot, ShipMatch and Claude.

```bash
docker compose -f docker-compose.yml -f docker-compose.mocks.yml up -d --build
docker compose cp dev/mocks/n8n-credentials.json n8n:/tmp/creds.json
docker compose exec n8n n8n import:credentials --input=/tmp/creds.json
scripts/import_workflows.sh docker-compose.mocks.yml
python dev/e2e_wf2.py --approve      # one email per category; clicks Approve on each Slack card
```

**Production:** one VPS, HTTPS through Caddy, backups: see [`docs/deploy.md`](docs/deploy.md).

## Evaluation

```bash
docker compose exec app python manage.py run_eval            # all 62 emails, with drafts
docker compose exec app python manage.py run_eval --limit 5  # quick check
docker compose exec app python manage.py run_eval --tag injection --no-drafts
```

Writes `app/evals/reports/<timestamp>.json` + `.html` (also under **Evaluations** in the dashboard) with the
metrics, a confusion matrix and every failure (expected vs actual). Each dataset file in `app/evals/dataset/`
is one email with its hand-checked label; review every new file by hand.

## Internal API (n8n → Django)

Every route needs `X-Internal-Token: $INTERNAL_TOKEN`; anything else gets 401.

| Route | Purpose |
|---|---|
| `GET /internal/health` | Health + email count |
| `POST /internal/emails` | Insert-or-ignore on `gmail_message_id` → `{id, created, status}` |
| `GET /internal/emails/{id}` | Email summary for n8n |
| `POST /internal/emails/{id}/triage` | Clean → classify → extract → validate → save `Triage` → `{category, urgency, fields, missing_fields, route, review_reason}` |
| `POST /internal/emails/{id}/draft` | Draft the reply, run the output checks → `awaiting_approval` (or `needs_review` if blocked); repeat returns the saved draft |
| `POST /internal/emails/{id}/approval` | Record approve/edit/reject → returns the reply as a raw Gmail message; disallowed decisions return 409 |
| `POST /internal/emails/{id}/status` | State change; disallowed transitions return 409 |
| `POST /internal/emails/{id}/actions/claim` | Reserve an idempotency key before an outside call → `{proceed, done, response}` |
| `GET/POST /internal/emails/{id}/actions` | Log outside actions with a unique idempotency key; `?key=` checks whether one was done |
| `POST /internal/failures` | WF4 error handler: finds the email (by ID or by the execution that last touched it), marks it `failed`, frees its claims, returns the Slack alert text |
| `GET /internal/metrics?period=today\|7d\|30d` | Metrics + digest text for WF5 |

`GET /healthz` is an unauthenticated liveness probe for Docker.

## Data retention

`python manage.py purge_old_bodies` deletes email bodies, drafts and sent text older than
`EMAIL_BODY_RETENTION_DAYS` (default 90) and keeps every row, status, cost and action, so metrics and the
audit trail stay. Run it daily from cron: `docker compose exec -T app python manage.py purge_old_bodies`.

## Development

```bash
cd app
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
# needs a Postgres the "inbox" user can create test databases on
DATABASE_URL=postgres://inbox:inbox@localhost:5432/inbox pytest
```

LLM calls are mocked in tests. CI runs the same suite (`.github/workflows/tests.yml`).

To run the workflows end to end without any accounts, use the mock services: see
"Running offline with mocks" in [`n8n/README.md`](n8n/README.md).

## Layout

```
app/            Django project (config/, inbox/, evals/, tests/)
  inbox/llm/    client.py (Anthropic wrapper), schemas.py, prompts/, triage.py, guard.py
n8n/workflows/  exported workflow JSON
docker/         Postgres init script
scripts/        init_env, import_workflows, backup, restore
docs/           architecture, deploy guide, case study, demo script, screenshots
dev/            mock services + e2e script for offline testing (not used in production)
```

## Project status

| Milestone | State |
|---|---|
| M0–M5 | Built and verified end to end against the offline mocks |
| M6 | Dataset (62), production overlay, scripts, docs, screenshots done; **pending**: first evaluation run with an API key, live deployment, demo video |

Full specification and every decision made while building: [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md) (Section 21).
