# AI Ops Inbox

AI that reads a company's shared inbox, works out what each email needs, puts the data into the right
systems, drafts the reply, and asks a person to approve before anything is sent. n8n orchestrates;
Django holds the AI logic, state and UI. Demo company: **Indus Freight** (a made-up freight broker).

The full specification is in [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md). This README covers setup and status;
the portfolio README is written at M6.

## Status

| Milestone | State |
|---|---|
| M0 Skeleton | Done: compose stack healthy, models, admin, `/internal/health` with token check, pytest |
| M1 Intake | Django side done and tested; WF1 exported. Needs a Gmail test account + OAuth in n8n to finish |
| M2 Triage | Code, prompts, 22 labelled emails and `run_eval` done; needs an `ANTHROPIC_API_KEY` run to check the ≥85% target |
| M3 Routing and actions | WF2 built and verified end to end against mocks (HubSpot, ShipMatch on/off, Slack, review routing, idempotency under parallel runs). Needs real HubSpot/Slack/ShipMatch credentials for the live check |
| M4–M6 | Not started |

## Quick start

```bash
cp .env.example .env        # fill in every blank value (see below)
docker compose up -d --build
docker compose exec app python manage.py createsuperuser
```

| URL | What |
|---|---|
| http://localhost:8001/ | Dashboard (log in with the superuser) |
| http://localhost:8001/admin/ | Django admin |
| http://localhost:5678/ | n8n editor |

Generate secrets with `openssl rand -hex 32` for `DJANGO_SECRET_KEY`, `INTERNAL_TOKEN`,
`N8N_ENCRYPTION_KEY`, `N8N_WEBHOOK_SECRET`, and passwords for `POSTGRES_PASSWORD`,
`DB_POSTGRESDB_PASSWORD`, `INBOX_DB_PASSWORD`. **Back up `N8N_ENCRYPTION_KEY`**: without it n8n can't
decrypt its stored credentials.

Then import the workflows: see [`n8n/README.md`](n8n/README.md).

## Internal API (n8n → Django)

Every route needs `X-Internal-Token: $INTERNAL_TOKEN`; anything else gets 401.

| Route | Purpose |
|---|---|
| `GET /internal/health` | Health + email count |
| `POST /internal/emails` | Insert-or-ignore on `gmail_message_id` → `{id, created, status}` |
| `GET /internal/emails/{id}` | Email summary for n8n |
| `POST /internal/emails/{id}/triage` | Clean → classify → extract → validate → save `Triage` → `{category, urgency, fields, missing_fields, route, review_reason}` |
| `POST /internal/emails/{id}/status` | State change; disallowed transitions return 409 |
| `POST /internal/emails/{id}/actions/claim` | Reserve an idempotency key before an outside call → `{proceed, done, response}` |
| `GET/POST /internal/emails/{id}/actions` | Log outside actions with a unique idempotency key; `?key=` checks whether one was done |
| `POST /internal/failures` | WF4 error handler; marks the email `failed` |

`GET /healthz` is an unauthenticated liveness probe for Docker.

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

## Evaluation

```bash
docker compose exec app python manage.py run_eval            # whole dataset
docker compose exec app python manage.py run_eval --limit 5  # quick check
docker compose exec app python manage.py run_eval --tag injection
```

Runs the production triage code over `app/evals/dataset/*.json` and writes
`app/evals/reports/<timestamp>.json` + `.html` (also listed under **Evaluations** in the dashboard).
It prints category accuracy, macro F1, field accuracy, invented values, injection catch rate, review rate,
latency p50/p95, cost per email, a confusion matrix and every failure.

Each dataset file holds one email and its hand-checked label (`expected`). Labels are only as good as the
person who checked them: review every new file by hand.

## Layout

```
app/            Django project (config/, inbox/, evals/, tests/)
  inbox/llm/    client.py (Anthropic wrapper), schemas.py, prompts/, triage.py, guard.py
n8n/workflows/  exported workflow JSON
docker/         Postgres init script
dev/            mock services + e2e script for offline testing (not used in production)
```
