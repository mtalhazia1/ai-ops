# Code review — 2026-10-16

Scope: the last commit's changes (`HEAD~1..HEAD`: refused ShipMatch uploads → review, n8n template), plus the
current `Caddyfile`, `docker-compose.prod.yml`, `scripts/` and `n8n/workflows/02_process.json`.
This was **not** a line-by-line audit of the whole codebase. Findings were reasoned from the code, not reproduced
by running it; #1, #4, #9 and #10 were spot-checked by reading the code.

| # | Severity | File | Issue |
|---|---|---|---|
| 1 | High | `docker-compose.prod.yml:40` (and `docker-compose.yml:8`) | Sets `N8N_WEBHOOK_URL`, but n8n reads `WEBHOOK_URL`. In production the Slack Approve/Reject links point at `https://<domain>:5678/`, a port Caddy doesn't expose, so every approval times out. |
| 2 | Medium | `app/inbox/services.py:96` | `failed_uploads` skips pending claims, so a draft can be written while another run's upload is still in flight; if that upload is later refused, the "documents received" draft is already awaiting approval. `test_successful_or_pending_uploads_still_draft` locks this in. |
| 3 | Medium | `n8n/workflows/02_process.json:1431` | "Ready for draft" is fed by both `Upload?[1]` and "Log upload", so with mixed attachments `/draft` can run before a refused upload is logged. The rerun of `/draft` then returns the earlier ok draft. |
| 4 | Medium | `app/inbox/views.py:194` | The refused-upload check is only in the API's `draft_email`; the dashboard Re-draft calls `services.create_draft` directly and skips it. The check belongs inside `create_draft`. |
| 5 | Medium | `app/inbox/services.py:87` | Any non-200/201 upload counts as a permanent refusal, including transient 429/5xx. A brief ShipMatch outage becomes a manual-review item that Retry can't recover (Retry only works from `failed`). |
| 6 | Low | `n8n/template/gmail-claude-slack-approval.json:168` (and 209) | The Slack message text contains a literal `\n` outside `{{ }}`, so Slack shows `\n` instead of line breaks. |
| 7 | Low | `app/inbox/api.py:253` | After the refused-upload path moves the email to `needs_review`, a repeated `/draft` (WF2 rerun) returns 409 instead of the same answer, so the review alert is never sent on rerun. |
| 8 | Low | `app/inbox/services.py:99` | The refusal text can read `HTTP None` with a raw Gmail attachment id, and the order is unstable (no `order_by`). |
| 9 | Low | `scripts/import_workflows.sh:22` | Publish failures are hidden (`>/dev/null` + `&& echo`), so the script prints "Done." while WF4 (the error workflow) may be unpublished. |
| 10 | Low | `scripts/backup.sh:19` | Retention cleanup pipes `ls` into `xargs rm -rf`, which splits on spaces; with a backup path containing a space, old backups aren't deleted and the wrong paths may be targeted. |

## Fix plan

Fixes run as four separate agents, each in its own git worktree. Issues that touch the same files are grouped so
the fixes don't conflict:

- **Agent A:** #1 (compose env var)
- **Agent B:** #2, #3, #4, #5, #7, #8 (refused-upload handling in `services.py`, `api.py`, `views.py`, `02_process.json`)
- **Agent C:** #6 (n8n template)
- **Agent D:** #9, #10 (scripts)

## Status: all 10 fixed

On branch `fix/code-review-2026-10-16`. The combined branch passes 163 tests (155 before) and
`makemigrations --check`, and both workflow JSON files and both scripts parse.

| Commit | Fixes |
|---|---|
| Set WEBHOOK_URL (not N8N_WEBHOOK_URL) for n8n services | #1 |
| Fix refused-upload guard: pending/transient uploads, one draft call, all draft paths | #2, #3, #4, #5, #7, #8 |
| Template: use real line breaks in Slack messages | #6 |
| Fail loudly on workflow publish errors; make backup retention space-safe | #9, #10 |

Not yet verified:
- The WF2 change (#3, #5) imports cleanly into n8n 2.43.2, but no workflow has been run end to end.
  `dev/mocks/mock_services.py` always accepts uploads, so it can't simulate a refusal (402) or an outage (503) yet.
- `scripts/import_workflows.sh` still hides the output of `dc restart`.

---

# Second review (full codebase) — 2026-10-16

Scope: full current contents of `app/inbox`, `app/config`, all five `n8n/workflows`, `docker-compose.yml`,
`docker/`, `scripts/init_env.sh`, `scripts/restore.sh`, on branch `fix/code-review-2026-10-16`.
Findings were reasoned from the code, not reproduced; #11, #12, #13, #19 and #20 were spot-checked by reading the code.

| # | Severity | File | Issue |
|---|---|---|---|
| 11 | Medium | `app/inbox/cleaning.py:41` | A quote request laid out as `From: Lahore / To: Karachi / ... / Pickup date:` is mistaken for an Outlook quoted-reply header and cut off, so the lane details are lost and the draft asks for details the customer already gave. |
| 12 | High | `app/inbox/api.py:106` | `timezone.utc` no longer exists in Django 5.x. Any email whose Date header has no time zone (or `-0000`) makes intake return 500, and the email is never saved. |
| 13 | Medium | `app/inbox/services.py:81` | Retry moves the email from `failed` to `received` before calling n8n. If n8n is down, the email is stuck in `received` for good: no Retry button, not in the review queue. |
| 14 | Medium | `n8n/workflows/02_process.json:1684` | A 5xx/409 from `/draft` ends WF2 as a "success": no failure recorded, no alert, and the email sits in `triaged`, which isn't in the review queue. |
| 15 | Medium | `n8n/workflows/03_execute.json:154` | Any error from `/approval` (422/409/500) goes to an "already decided" no-op, so an approval can be silently dropped. `Mark done` has the same problem. |
| 16 | Medium | `app/inbox/api.py:318` | Slack Approve sends whatever the latest draft is at click time. If someone re-drafted in the dashboard meanwhile, the customer gets a reply nobody approved in Slack. |
| 17 | Low | `app/inbox/metrics.py:46` | An email that ever failed (even once, then retried and sent unchanged) is counted as "reviewed", not "automatic", so the dashboard and daily digest are skewed. |
| 18 | High | `app/inbox/api.py:491` | Recording a failure frees the `reply_sent` claim. If Gmail sent the reply but logging it failed, Retry sends the customer a second copy. |
| 19 | Medium | `app/inbox/api.py:126` | With several addresses in To (or an unparseable From), the raw header is stored instead. That can exceed 320 characters (intake 500, email lost), and a bad From becomes the reply recipient and HubSpot contact. |
| 20 | High | `app/inbox/management/commands/purge_old_bodies.py:29` | The 90-day purge also blanks drafts and approvals of emails that are still open. Retrying such an email can send the customer "[deleted after retention period]". |

## Fix plan (second round)

- **Agent E:** #11, #12, #19 (intake: `cleaning.py`, `api.py` create_email / `_parse_date`)
- **Agent F:** #13, #18, #20 (retry and send safety: `services.retry`, `api.record_failure` / claims, `purge_old_bodies`)
- **Agent G:** #14, #15, #16 (n8n error paths in WF2/WF3, and tying the Slack approval to the draft it showed)
- **Agent H:** #17 (metrics)
