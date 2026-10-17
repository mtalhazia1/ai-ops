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

Verified end to end on 2026-10-17 (see "End-to-end run" below): #1 (Slack resume links use `WEBHOOK_URL`),
#2, #3, #5, #7, #8 and #9 (the script now prints each publish) in n8n 2.43.2 against the mocks.

Still not verified:
- `scripts/import_workflows.sh` still hides the output of `dc restart`.
- #6 (template) and #10 (backup retention) were not run.

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

## Status (second round): all 10 fixed

On branch `fix/code-review-2026-10-16`. The combined branch (both rounds) passes 201 tests (155 before the reviews)
and `makemigrations --check` finds nothing missing (no new migrations); every workflow JSON parses, and WF2/WF3 import
into n8n 2.43.2.

| Commit | Fixes |
|---|---|
| Cleaning: only treat From: as an Outlook quote header when followed by header lines | #11 |
| Intake: fix naive Date headers crashing with Django 5 (timezone.utc removed) | #12 |
| Keep the email retryable when Retry can't reach n8n | #13 |
| WF2: stop with an error on any unexpected Django answer | #14 |
| WF3: only a real "already decided" answer ends quietly | #15 |
| Tie the Slack approval to the draft its card showed | #16 |
| Metrics: count reviewed by human involvement, not needs_review_reason | #17 |
| Never resend a reply whose send may already have gone out | #18 |
| Intake: parse To/From/Reply-To with getaddresses; never reply to an unparseable sender | #19 |
| Purge only closed emails; never send a purged reply | #20 |

New behaviour to know about:
- #18: when it isn't known whether a reply went out, the email shows **Mark as sent** / **Send again anyway** in the
  dashboard instead of Retry. A Gmail error before sending also lands here (it can't be told apart from "sent, but
  logging failed").
- #19: an email whose sender address can't be parsed is saved and sent to review; it can't be replied to or added to HubSpot.

Verified end to end on 2026-10-17 (see below): #14, #15, #16, #17 (one case), #18, and #19 as far as WF3 goes
(no address → 422 → failure). The run found that the #16 change had broken every Slack approval (fixed, see below).

Known gaps:
- No Slack message when a Slack approval is refused as stale (#16); it shows up in the dashboard review queue.
  An unconfirmed send (#18) moved to failed by a WF3 error gets WF4's `#ops-alerts` message (verified); one
  moved there by an expired claim still has no message.
- `metrics.REVIEW_ALERT_KEY` must match WF2's `slack_alert:<id>:review` key (it does today).
- Not run end to end: #11, #12, #13 (n8n down during Retry), #20 (purge); they are covered by pytest only.

---

# End-to-end run against the mocks — 2026-10-17

The n8n paths changed by both reviews had only been imported. This run executed them in n8n 2.43.2 (queue mode,
one worker) against `dev/mocks/mock_services.py`, with `SHIPMATCH_ENABLED=true` and `APPROVAL_TIMEOUT_MINUTES=1`.

The mocks gained failure controls (upload filename markers `refuse400` / `paused402`, and `POST /_control`
rules for 4xx/5xx answers, delays and a malformed Anthropic answer), and `dev/e2e_scenarios.py` scripts the
scenarios (see `n8n/README.md`, "Running offline with mocks"). On a fresh stack all 11 scenarios pass:

| Scenario | Checks | Result |
|---|---|---|
| `happy` | 7 categories × 3 overlapping WF2 runs: 1 deal, 1 note, 1 task, 2 uploads (`.exe` skipped), 1 urgent alert, 1 review alert each for other/injection, 1 card each; Approve → 1 Gmail send + `ai-processed`; Reject → nothing sent + `ai-rejected`; no Failure | PASS |
| `late_rerun` | WF2 rerun on an awaiting-approval and on a rejected email ends at *Already processed*, status unchanged | PASS (after fix B) |
| `refusal` (#2/#5/#7/#8) | 402 + 400 → `needs_review`, reason names both files (no attachment ids, no `HTTP None`), blocked draft with empty body, no drafting LLM call, 1 review alert; rerun → `/draft` answers the same `ok:false, rerun:true`, no new draft, no second alert | PASS |
| `retry` (#5, #3) | 503 upload → WF2 error at *ShipMatch upload failed*, Failure + `#ops-alerts`, email `failed`, good upload kept, flaky claim released; dashboard Retry → only the flaky file re-uploaded, *Ready for draft* and `/draft` once, after *Log upload*; failure resolved; card posted | PASS |
| `draft_errors` (#14) | Anthropic 500 → Django saves a blocked draft ("drafting failed: InternalServerError…") → review + alert, WF2 succeeds. Malformed Anthropic answer → `/draft` 500 HTML → WF2 error at *Draft failed*, Failure, alert; Retry reuses the triage (1 LLM call) → ok draft + card | PASS |
| `approval_errors` (#15) | `/approval` 422 (unparseable sender) → WF3 error at *Decision not recorded* with Django's reason, Failure, nothing sent; dashboard Reject then the old Slack Approve → WF3 ends at *Already decided otherwise*, no Failure; WF2 on a `failed` email → error at *Triage failed* | PASS |
| `stale` (#16) | Re-draft in the dashboard, then the old Slack Approve → 409 `stale_draft`, WF3 ends quietly, nothing sent, no Approval, email in review; dashboard Approve → exactly 1 reply with the new draft | PASS (after fix A) |
| `send_uncertain` (#18) | Gmail 503 after the claim → WF3 error, email `failed` with "reply may already have been sent", claim `uncertain`, alert; dashboard Retry refused and starts no WF3; page shows *Mark as sent* / *Send again anyway*; *Send again anyway* → exactly 1 reply, `done` | PASS |
| `race` (#2) | Two runs on one paperwork email, first upload slow (8 s) and refused (402): second run ends at *Another run drafts* (`pending:true`), first drafts blocked → review; no ok draft, no card | PASS |
| `timeout` | No click → after 1 min *Approval timed out* → `needs_review` + 1 review alert; a dashboard Approve meanwhile → *Already decided*, still 1 reply | PASS |
| `metrics` (#17) | `/internal/metrics?period=today` matches the database (handled, replies, failures, open failures, queues, rejected); a reply sent after a failure counts as automatic; WF5 *Run now* posts the same digest text | PASS |

`pytest` (CI way: postgres:17, python:3.12-slim): 201 passed; `makemigrations --check`: no changes.

## Bugs found and fixed

- **A. Slack "Approve and send" always failed (regression from #16).** `/draft` returns `draft_id` as a number,
  WF3's input declares it a string, and n8n 2.43.2 rejects the sub-workflow call ("Invalid input for 'draft_id'").
  Nothing was recorded or sent; when an overlapping run had touched the email last, WF4 couldn't even tie the
  failure to it, so the email sat in `awaiting_approval`. Fix: WF2 passes `String(draft_id)`.
- **B. A late WF2 run failed a healthy email.** `/triage` answers 409 for an email past triage; the node threw and
  WF4 moved the email to `failed`. `dev/e2e_wf2.py --runs 3` hit it for most emails (with more runs than worker
  slots, queued runs start after the first has drafted); the email's live Slack card then failed too. The same
  happened at `/draft` (409 `needs_review`) for an email in review with an ok draft. Fix: WF2 reads `/triage`
  like `/draft`: 409 naming `awaiting_approval`/`approved`/`done`/`rejected`/`ignored` → *Already processed*,
  anything else → *Triage failed* (with Django's detail, not n8n's generic 409 text); `/draft` 409 naming
  `needs_review`/`rejected`/`ignored` → *Left to the reviewer*.
- **C. The stack didn't start from a Windows checkout.** With `core.autocrlf=true`, `docker/postgres-init.sh`
  had CRLF line endings, postgres couldn't run it, and the databases were never created. Fix: `.gitattributes`
  (`*.sh text eol=lf`).

## Observations (not changed)

- An Anthropic outage while drafting sends the email to review (blocked draft "drafting failed"), not to
  `failed`; Retry doesn't apply, the reviewer uses Re-draft. That is Django's documented behaviour for LLM
  errors, unlike #5's rule for ShipMatch outages; worth a decision.
- Metrics: an email whose Slack approval was refused as stale (dashboard Re-draft, then dashboard Approve) counts
  as "automatic": the stale path moves it to review without a review alert, and nothing else records that a
  person re-drafted it.
- WF4 finds the email by the last n8n execution that called Django for it. A resumed approval run that fails
  before calling Django again (as in A), after an overlapping run touched the email, records an unattached
  Failure.
- n8n 2.43.2 logs "WEBHOOK_URL -> Use N8N_WEBHOOK_URL instead", but honours `WEBHOOK_URL` (the run set it to
  `http://127.0.0.1:5678/` and the Slack links used it). #1's fix works; the variable may need renaming on a later
  n8n upgrade.
