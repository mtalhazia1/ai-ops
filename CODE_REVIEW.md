# Code review — 2026-10-16

Scope: the last commit's changes (`HEAD~1..HEAD`: refused ShipMatch uploads → review, n8n template), plus the
current `Caddyfile`, `docker-compose.prod.yml`, `scripts/` and `n8n/workflows/02_process.json`.
This was **not** a line-by-line audit of the whole codebase. Findings were reasoned from the code, not reproduced
by running it; #1, #4, #9 and #10 were spot-checked by reading the code.

| # | Severity | File | Issue |
|---|---|---|---|
| 1 | ~~High~~ false positive | `docker-compose.prod.yml:40` (and `docker-compose.yml:8`) | Sets `N8N_WEBHOOK_URL`, but n8n reads `WEBHOOK_URL`. In production the Slack Approve/Reject links point at `https://<domain>:5678/`, a port Caddy doesn't expose, so every approval times out. **Wrong:** tested on 2026-10-17, n8n 2.43.2 honours both names and `WEBHOOK_URL` is the deprecated one (see the 2026-10-17 observations section). |
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
| Set WEBHOOK_URL (not N8N_WEBHOOK_URL) for n8n services (back to `N8N_WEBHOOK_URL` on 2026-10-17: false positive) | #1 |
| Fix refused-upload guard: pending/transient uploads, one draft call, all draft paths | #2, #3, #4, #5, #7, #8 |
| Template: use real line breaks in Slack messages | #6 |
| Fail loudly on workflow publish errors; make backup retention space-safe | #9, #10 |

Verified end to end on 2026-10-17 (see "End-to-end run" below): #2, #3, #5, #7, #8 and #9 (the script now prints
each publish) in n8n 2.43.2 against the mocks. #1 turned out to be a false positive: n8n 2.43.2 reads both
`N8N_WEBHOOK_URL` and the deprecated `WEBHOOK_URL`, so the original setting already worked.

Still not verified: ~~#6 (template) and #10 (backup retention) were not run~~ — both run on 2026-10-20, see
"Third end-to-end run" at the end.
- (`scripts/import_workflows.sh` hiding the output of `dc restart`: fixed and run on 2026-10-17.)

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
- ~~`metrics.REVIEW_ALERT_KEY` must match WF2's `slack_alert:<id>:review` key~~ — gone on 2026-10-17: metrics read
  `Email.needed_review_at` instead.
- ~~Not run end to end: #11, #12, #13 (n8n down during Retry), #20 (purge); they are covered by pytest only.~~
  All four run end to end on 2026-10-20, see "Third end-to-end run" at the end.

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

## Observations — resolved on 2026-10-17

All four observations below were fixed (or, for the last one, settled by testing) on branch
`fix/code-review-2026-10-16`; see "Observations: fixes and second end-to-end run" at the end.

- ~~An Anthropic outage while drafting sends the email to review (blocked draft "drafting failed"), not to
  `failed`; Retry doesn't apply, the reviewer uses Re-draft.~~ **Resolved:** an outage now fails the run and
  Retry repeats it, during triage as well as drafting.
- ~~Metrics: an email whose Slack approval was refused as stale (dashboard Re-draft, then dashboard Approve) counts
  as "automatic".~~ **Resolved:** the email records that it was in review (`needed_review_at`).
- ~~WF4 finds the email by the last n8n execution that called Django for it. A resumed approval run that fails
  before calling Django again, after an overlapping run touched the email, records an unattached Failure.~~
  **Resolved:** every execution id per email is kept.
- ~~n8n 2.43.2 logs "WEBHOOK_URL -> Use N8N_WEBHOOK_URL instead", but honours `WEBHOOK_URL`.~~ **Settled:** both
  names work; the compose files are back on `N8N_WEBHOOK_URL`, and round-1 finding #1 was a false positive.

---

# Observations: fixes and second end-to-end run — 2026-10-17

| Commit | Change |
|---|---|
| Fail the run on an Anthropic outage instead of routing to review | Obs. 1 |
| Metrics: record on the email that it needed review | Obs. 2 |
| WF4: find the email by any execution that touched it | Obs. 3 |
| n8n: set N8N_WEBHOOK_URL, the non-deprecated name | Obs. 4, corrects #1 |
| import_workflows.sh: fail loudly when the n8n restart fails | leftover from round 1 |
| WF2: post a new approval card for a new draft after Retry | new bug D (below) |

1. **Anthropic outage → failed + Retry.** `inbox/llm/client.py` raises `LLMUnavailable` (a subclass of
   `LLMError`) for a connection error, timeout, 408, 429 or 5xx (incl. 529 overloaded) once the SDK's two retries
   are used up. `triage()` and `draft_reply()` re-raise it instead of returning a review route / blocked draft.
   `/triage` and `/draft` answer 503 `{ok:false, retry:true, detail:"AI service unavailable, try again later
   (…)"}` and save nothing (no Triage row, no Draft, status unchanged). WF2 already sent those answers to
   *Triage failed* / *Draft failed*; *Triage failed* now words the message like *Draft failed* ("Triage not
   possible now: …"). WF4 records the Failure and moves the email to `failed`; Retry repeats only what is
   missing (after a drafting outage the saved triage is reused). Other API errors (400, 401, …), refusals,
   truncation and invalid output still go to review as before. The dashboard Re-draft says "Not re-drafted: the
   AI service is unavailable right now (…)" and keeps the current draft.
2. **`Email.needed_review_at`** (migration 0004): set by `state.transition` the first time an email enters
   `needs_review`, never cleared. `metrics.needed_human` = `needed_review_at` set, or a human triage row, a blocked
   draft (a dashboard Re-draft can block one without a status change), or an edited approval.
   `REVIEW_ALERT_KEY` is gone. The migration backfills from review-routed triages, blocked drafts, review-alert
   actions, emails in review now, and the stale-approval / approval-timeout reasons.
3. **`EmailExecution`** (migration 0005; unique per execution id and email): every n8n execution id that called
   the internal API for an email. The insert happens when the email's `last_execution_id` changes, so it costs one
   write per run, not per call; intake records its execution too. `/internal/failures` looks the email up there.
   An execution that touched more than one email (a WF1 batch) is still not attached to any — "latest wins" would
   fail a healthy email there. The migration seeds the table from `last_execution_id`, which stays.
4. **`WEBHOOK_URL` vs `N8N_WEBHOOK_URL`, tested.** Throwaway `n8nio/n8n:2.43.2` (sqlite), three runs; the resume
   URL was read from a Code node returning `$execution.resumeUrl` (imported workflow run with `n8n execute`),
   plus the startup log:

   | Environment | Startup log | `$execution.resumeUrl` |
   |---|---|---|
   | only `N8N_WEBHOOK_URL=https://example.test/` | "Editor is now accessible via: https://example.test", no warning | `https://example.test/webhook-waiting/1?signature=…` |
   | only `WEBHOOK_URL=https://example.test/` | same, plus "WEBHOOK_URL -> Use N8N_WEBHOOK_URL instead" | `https://example.test/webhook-waiting/1?signature=…` |
   | neither | "http://localhost:5678" | `http://localhost:<port>/webhook-waiting/1?signature=…` |

   Both work, so #1 was a false positive (the original `N8N_WEBHOOK_URL` was fine). `docker-compose.yml` and
   `docker-compose.prod.yml` set `N8N_WEBHOOK_URL` again; `docker-compose.yml` falls back to an older `.env`'s
   `WEBHOOK_URL`. `docker compose config` (dummy env outside the repo) shows `N8N_WEBHOOK_URL` on n8n and
   n8n-worker: `http://localhost:5678/` by default, `https://<N8N_DOMAIN>/` with the prod overlay, and the legacy
   value when only `WEBHOOK_URL` is set.
5. `scripts/import_workflows.sh` prints "Restarted n8n and n8n-worker", or the restart's output and exits 1.

## Bugs found and fixed

- **D. No Slack card after Retry of an email whose card was already posted.** WF2 claimed the card once per email
  (`slack_alert:<id>:approval`). When a run failed after posting its card (the `resumed_failure` scenario), Retry
  drafted again but the claim said *done*: no new card, and the old card's buttons pointed at the finished
  execution, so the email could only be approved in the dashboard. The key now ends in the draft id
  (`slack_alert:<id>:approval:<draft_id>`): overlapping runs still share one card (they get the same saved draft),
  a new draft gets its own, and an older card's Approve is refused as stale (#16).
- **E. Stale CRLF in an older worktree.** A worktree checked out before `.gitattributes` (bug C) kept CRLF in
  `docker/postgres-init.sh` and postgres created no databases. Not a repository problem (the index is LF);
  re-checking out the scripts fixes it.

## Second end-to-end run (fresh `aiops-obs` stack, n8n 2.43.2 queue mode, mocks)

`dev/e2e_scenarios.py` gained the scenario `resumed_failure`, and `draft_errors` / `metrics` check the new
behaviour. All 12 scenarios pass:

| Scenario | Checks | Result |
|---|---|---|
| `happy` | as before (7 categories × 3 runs; one card each with the per-draft claim key) | PASS |
| `late_rerun` | as before | PASS |
| `refusal` | as before | PASS |
| `retry` | as before | PASS |
| `draft_errors` (obs. 1, #14) | (a) Anthropic 500 while drafting → `/draft` 503 `retry:true` ("AI service unavailable … InternalServerError"), WF2 error at *Draft failed*, email `failed`, no Draft row, `needed_review_at` empty, 1 Failure, 1 `#ops-alerts`, no review alert; Retry → 1 LLM call (triage reused), ok draft + card, failure resolved. (c) Anthropic 529 while classifying → `/triage` 503, WF2 error at *Triage failed* ("Triage not possible now: … OverloadedError"), no Triage row; Retry → 3 LLM calls, one Triage row, card. (b) malformed answer → `/draft` 500 → *Draft failed*, Retry reuses the triage | PASS |
| `approval_errors` | as before | PASS |
| `stale` | as before | PASS |
| `send_uncertain` | as before | PASS |
| `race` | as before | PASS |
| `timeout` | as before | PASS |
| `resumed_failure` (obs. 3) | Overlapping WF2 run → email's last execution is the overlapping one; the card's run resumes after Approve and fails at *Send reply (WF3)* ("Invalid input for 'draft_id'", bug A replayed in that execution's stored copy of WF2) before calling Django → Failure attached to the email with the resumed run's execution id, email `failed`, nothing sent, 1 `#ops-alerts`, no unattached Failure; Retry → new card (bug D fix), Approve → 1 reply, failure resolved | PASS |
| `metrics` (obs. 2, #17) | counters match the database; automatic/reviewed (4/14) match the same definition computed in SQL with `needed_review_at`; the `stale` email (re-drafted, Slack approval refused, dashboard Approve unchanged, no review alert) has `needed_review_at` and counts as reviewed; WF5 digest text matches | PASS |

`pytest` (CI way: postgres:17, python:3.12-slim): 223 passed (201 before); `makemigrations --check`: no changes.

---

# Third end-to-end run: the items not yet run — 2026-10-20

Fresh stack (`scripts/init_env.sh`, mocks, n8n 2.43.2 queue mode, `SHIPMATCH_ENABLED=true`,
`APPROVAL_TIMEOUT_MINUTES=1`). `dev/e2e_scenarios.py` gained four scenarios (`cleaning`, `naive_date`,
`retry_n8n_down`, `purge`). The full suite (16) ran: 14 passed, `purge` and `metrics` failed on test-only
interference (see Notes); after that fix the five affected scenarios were re-run and pass.

| Commit | Change |
|---|---|
| Run the untested review items end to end; fix the template's Parse result | #6, #10, #11, #12, #13, #20; bug F |

| Item | How it was run | Result |
|---|---|---|
| #11 cleaning | `cleaning`: intake of a quote laid out as `From: Lahore` / `To: Karachi` / `Weight` / `Pieces` / `Pickup date`, and of a real Outlook reply (`From: Name <addr>` / `Sent:` / `To:` / `Subject:`), then WF2 and Approve | PASS: lane kept in `body_clean`; triage `quote_request`, origin Lahore / destination Karachi extracted, missing only `pickup_date` (the mock extractor doesn't read dates); draft doesn't ask for the lane; the Outlook reply is cut to its first line |
| #12 naive Date | `naive_date`: intake with `-0000`, no zone, naive ISO and `+0500` | PASS: all four intake 200 and saved as 10:15 UTC; each runs to an approval card and is sent on Approve; no Failure |
| #13 Retry, n8n down | `retry_n8n_down`: email failed by an Anthropic draft 500, `docker compose stop n8n`, dashboard Retry, `start n8n`, Retry again | PASS: flash "Retry failed: n8n unreachable: …", email still `failed`, Failure open, Retry still offered and listed on Failures; after the restart Retry → card, Failure resolved, Approve sends once |
| #20 purge | `purge`: four emails backdated 100 days (one done, one rejected, two awaiting approval), `manage.py purge_old_bodies` | PASS: only the done and rejected ones blanked (body, draft, approval; status and actions kept); a second purge finds nothing; the old open email's Slack Approve sends its real draft; a draft blanked anyway (set by SQL) → `/approval` 409 "reply text was deleted…", WF3 error, Failure, nothing sent |
| #6 template | Imported the template unchanged (imports). Ran a harness built from it: Gmail trigger and Claude replaced by Code nodes with fixed data, the two Slack nodes by HTTP requests to the mock Slack with the template's own message expressions | PASS after bug F: real line breaks in both messages (8 and 3), no literal `
`. The pre-fix file (`eeedddb`) sends literal `
`. Real Gmail, Claude and Slack *send and wait* not run (needs real credentials) |
| #10 backup retention | `scripts/backup.sh "<dir>/back ups"` with 16 stamp dirs, a `manual copy` dir, a file, and sibling dirs `back/` and `ups/` holding stamp dirs | PASS: dumps written; 14 newest stamp dirs kept (3 oldest removed); `manual copy`, the file and the sibling dirs untouched |

## Bugs found and fixed

- **F. The template failed on every email.** *Parse result* runs once for each item but returned an array;
  n8n 2.43.2 stops with "A 'json' property isn't an object [item 0]", so no Slack message was ever sent. It
  now returns one object. (Present since the template was added; found only by running it.)
- **E again.** The main checkout still had CRLF in the `.sh` files (postgres created no databases); re-checking
  them out fixed it, as before.

## Notes

- The new scenarios approve their emails at the end, so none times out into review while `metrics` runs (the
  first full run failed `purge` and `metrics` on that and on a closed email left by an earlier run; test-only).
- `purge` backdates emails, so the digest's "Average time to reply" is large after it runs.
- An email whose draft was purged lands in `failed`; Retry fails the same way, so the reviewer has to Re-draft.
- From Git Bash on Windows, `scripts/import_workflows.sh` needs `MSYS_NO_PATHCONV=1` (otherwise `/workflows/...`
  becomes `C:/Program Files/Git/workflows/...`).

