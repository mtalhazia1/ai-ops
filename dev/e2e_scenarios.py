"""Scripted end-to-end scenarios for WF2–WF5 against the mocks, including the failure paths
(development only). Prints PASS/FAIL per scenario with the evidence it checked.

    docker compose -f docker-compose.yml -f docker-compose.mocks.yml up -d
    python dev/e2e_scenarios.py                 # every scenario
    python dev/e2e_scenarios.py refusal retry   # only these (names: see SCENARIOS at the bottom)
    python dev/e2e_scenarios.py --list

Needs the same setup as dev/e2e_wf2.py (mock credentials and WF2–WF5 imported and published,
see n8n/README.md "Running offline with mocks"), plus in .env: SHIPMATCH_ENABLED=true and
APPROVAL_TIMEOUT_MINUTES=1 (the timeout scenario waits for it). It reads the Django and n8n
databases through `docker compose exec postgres psql`, drives failures through the mocks'
POST /_control, and creates a dashboard superuser `e2e` (password from E2E_PASSWORD, default
"e2e-pass-123") to press Retry, Re-draft, Approve and "Send again anyway" like a reviewer.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_wf2 import APP, BROWSER_UA, COMPOSE, ENV, EMAILS, N8N, attachment, request  # noqa: E402

PASSWORD = os.environ.get("E2E_PASSWORD", "e2e-pass-123")
STAMP = int(time.time())
WF = {"wf2": "wf2process000001", "wf3": "wf3execute000001", "wf4": "wf4error00000001", "wf5": "wf5digest0000001"}
SENDER = "Sarah Khan <sarah@acme.example>"


# --- Plumbing ---------------------------------------------------------------------------

def dc(*args, stdin=None):
    out = subprocess.run(COMPOSE + list(args), input=stdin, capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        raise RuntimeError(f"docker compose {' '.join(args[:3])} failed: {out.stderr[-500:]}")
    return out.stdout


def sql(db, query):
    """Rows of `query` as dicts, read with psql inside the postgres container."""
    wrapped = f"select coalesce(json_agg(t), '[]'::json) from ({query}) t"
    return json.loads(dc("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", db, "-At", "-c", wrapped) or "[]")


MOCK_CALL = """
import json, sys, urllib.request
path, data = sys.argv[1], sys.stdin.read()
req = urllib.request.Request('http://127.0.0.1:9000' + path, data=data.encode() if data else None,
                             method='POST' if data else 'GET')
print(urllib.request.urlopen(req).read().decode())
"""


def mock(path, data=None):
    return json.loads(dc("exec", "-T", "mocks", "python", "-c", MOCK_CALL, path,
                         stdin=json.dumps(data) if data is not None else ""))


def mock_state():
    return mock("/_state")


def faults(*rules):
    return mock("/_control", {"add": list(rules)})


def clear_faults():
    return mock("/_control", {"clear": True})


def wait_until(predicate, timeout=90, every=1.5, what="condition"):
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() > deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(every)


# --- n8n executions -----------------------------------------------------------------------

def unflatten(text):
    """n8n stores execution data in `flatted` format: a list whose first entry is the root,
    and every string inside an object or list is the index of another entry."""
    table = json.loads(text)
    done = {}

    def revive(index):
        if index in done:
            return done[index]
        value = table[index]
        if isinstance(value, dict):
            out = done[index] = {}
            for k, v in value.items():
                out[k] = revive(int(v)) if isinstance(v, str) else v
            return out
        if isinstance(value, list):
            out = done[index] = []
            out.extend(revive(int(v)) if isinstance(v, str) else v for v in value)
            return out
        return value

    return revive(0)


EMAIL_NODE = {WF["wf2"]: "Email ID", WF["wf3"]: "Decision", WF["wf4"]: "Record failure (Django)"}
_EXEC_CACHE: dict = {}


def first_json(run_data, node):
    try:
        return run_data[node][0]["data"]["main"][0][0]["json"]
    except (KeyError, IndexError, TypeError):
        return {}


def node_json(execution, node, run=0, output=0):
    try:
        return [i["json"] for i in execution["run_data"][node][run]["data"]["main"][output]]
    except (KeyError, IndexError, TypeError):
        return []


def executions(workflow, email_id=None, since=0):
    """Executions of `workflow` after id `since`, newest last, optionally only for one email."""
    rows = sql("n8n", f"""select e.id, e.status, e."startedAt", e."stoppedAt", d.data from execution_entity e
                          join execution_data d on d."executionId" = e.id
                          where e."workflowId" = '{WF.get(workflow, workflow)}' and e.id > {int(since)} order by e.id""")
    found = []
    for row in rows:
        key = (row["id"], row["status"])
        if key not in _EXEC_CACHE:
            root = unflatten(row["data"])
            result = root.get("resultData") or {}
            run_data = result.get("runData") or {}
            error = result.get("error") or {}
            _EXEC_CACHE[key] = {
                "id": row["id"], "status": row["status"], "run_data": run_data,
                "email_id": first_json(run_data, EMAIL_NODE.get(WF.get(workflow, workflow), "")).get("email_id"),
                "nodes": {name: len(runs) for name, runs in run_data.items()},
                "last": result.get("lastNodeExecuted"), "error": error.get("message"),
            }
        found.append(_EXEC_CACHE[key])
    return [e for e in found if email_id is None or e["email_id"] == email_id]


def last_execution_id():
    rows = sql("n8n", "select coalesce(max(id), 0) as id from execution_entity")
    return rows[0]["id"]


def busy():
    return sql("n8n", "select id from execution_entity where status in ('new', 'running')")


def quiet(timeout=120):
    """Wait until no n8n execution is running (twice in a row, so WF4 has had its turn)."""
    wait_until(lambda: not busy(), timeout, what="n8n executions to finish")
    time.sleep(2)
    wait_until(lambda: not busy(), timeout, what="n8n executions to finish")


def short(execution):
    return f"#{execution['id']} {execution['status']} at {execution['last']!r}" + (
        f" ({execution['error'][:120]})" if execution.get("error") else "")


# --- Django -------------------------------------------------------------------------------

def new_email(name, *, subject, text, attachments=(), sender=SENDER, date=None):
    status, body = request(f"{APP}/internal/emails", {
        "gmail_message_id": f"e2e-{STAMP}-{name}", "gmail_thread_id": f"t-{STAMP}-{name}",
        "from": sender, "subject": subject, "text": text, "message_id": f"<e2e-{STAMP}-{name}@acme.example>",
        "attachments": list(attachments), **({"date": date} if date else {})}, {"X-Internal-Token": ENV["INTERNAL_TOKEN"]})
    assert status == 200, (status, body)
    return body["id"]


def sql_exec(db, statement):
    """Run a write statement with psql inside the postgres container."""
    return dc("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-c", statement)


def run_wf2(email_id):
    status, _ = request(f"{N8N}/webhook/process", {"email_id": email_id}, {"x-webhook-secret": ENV["N8N_WEBHOOK_SECRET"]})
    assert status == 200, f"WF2 webhook answered {status}"


def run_wf3(email_id, decision, final_body="", reviewer="e2e-webhook"):
    status, _ = request(f"{N8N}/webhook/execute", {"email_id": email_id, "decision": decision, "reviewer": reviewer,
                                                    "channel": "dashboard", "final_body": final_body},
                        {"x-webhook-secret": ENV["N8N_WEBHOOK_SECRET"]})
    assert status == 200, f"WF3 webhook answered {status}"


def email(email_id):
    return sql("inbox", f"select * from inbox_email where id = {int(email_id)}")[0]


def status_of(email_id):
    return email(email_id)["status"]


def rows(table, email_id, order="id"):
    return sql("inbox", f"select * from inbox_{table} where email_id = {int(email_id)} order by {order}")


def wait_status(email_id, *statuses, timeout=90):
    return wait_until(lambda: status_of(email_id) in statuses and status_of(email_id),
                      timeout, what=f"email {email_id} to reach {'/'.join(statuses)}")


def slack_for(email_id, state=None, channel=None):
    state = state or mock_state()
    marker = f"/emails/{email_id}/"
    return [m for m in state["slack"]
            if marker in json.dumps([m.get("text"), m.get("blocks")]) and (channel is None or m["channel"] == channel)]


def sent_for(email_id, state=None):
    state = state or mock_state()
    return [s for s in state["sent"] if s["threadId"] == email(email_id)["gmail_thread_id"]]


def approval_buttons(email_id, state=None):
    for message in slack_for(email_id, state, "#ops-approvals"):
        if message.get("blocks"):
            return {b["text"]["text"]: b["url"] for b in message["blocks"][1]["elements"]}
    return {}


def wait_card(email_id, timeout=90):
    return wait_until(lambda: approval_buttons(email_id), timeout, what=f"approval card for email {email_id}")


def click(url):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class Dashboard:
    """A logged-in reviewer session (Django admin login, then the dashboard's own forms)."""

    def __init__(self):
        dc("exec", "-T", "-e", f"DJANGO_SUPERUSER_PASSWORD={PASSWORD}", "app", "sh", "-c",
           "python manage.py createsuperuser --noinput --username e2e --email e2e@example.com >/dev/null 2>&1 || true")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.get("/admin/login/")
        page = self.post("/admin/login/?next=/", {"username": "e2e", "password": PASSWORD, "next": "/"})
        assert "Review queue" in page or "Log out" in page, "dashboard login failed"

    def csrf(self):
        return next(c.value for c in self.jar if c.name == "csrftoken")

    def get(self, path):
        with self.opener.open(APP + path, timeout=30) as resp:
            return resp.read().decode()

    def post(self, path, fields):
        data = urllib.parse.urlencode({**fields, "csrfmiddlewaretoken": self.csrf()}).encode()
        req = urllib.request.Request(APP + path, data=data, headers={"Referer": APP + path})
        with self.opener.open(req, timeout=30) as resp:
            return resp.read().decode()

    @staticmethod
    def flashes(page):
        return [re.sub(r"\s+", " ", m) for m in re.findall(r'<p class="flash[^"]*">(.*?)</p>', page, re.S)]


_DASHBOARD = None


def dashboard():
    global _DASHBOARD
    if _DASHBOARD is None:
        _DASHBOARD = Dashboard()
    return _DASHBOARD


# --- Checks ---------------------------------------------------------------------------------

class Result:
    def __init__(self, name):
        self.name, self.lines, self.ok = name, [], True

    def check(self, condition, message):
        self.lines.append(("ok  " if condition else "FAIL") + "  " + message)
        self.ok = self.ok and bool(condition)
        return condition

    def note(self, message):
        self.lines.append("      " + message)


def actions_by_key(email_id):
    return {a["idempotency_key"]: a for a in rows("action", email_id)}


QUOTE = EMAILS["quote"]


def quote_email(name, subject=None):
    return new_email(name, subject=subject or f"rate pls {name}", text=QUOTE["text"])


# --- Scenarios --------------------------------------------------------------------------------

def s_happy(r):
    """1. Every category, three overlapping WF2 runs each; approve the quote, reject the booking."""
    mock("/_reset", {})
    since = last_execution_id()
    before = mock_state()
    ids = {}
    for name in ("quote", "booking", "status", "paperwork", "claim", "other", "injection"):
        e = EMAILS[name]
        ids[name] = new_email(f"happy-{name}", subject=e["subject"], text=e["text"], attachments=e.get("attachments", []))
    for _ in range(3):
        for email_id in ids.values():
            run_wf2(email_id)
    expected = {"quote": "awaiting_approval", "booking": "awaiting_approval", "status": "awaiting_approval",
                "paperwork": "awaiting_approval", "claim": "awaiting_approval", "other": "needs_review",
                "injection": "needs_review"}
    for name, email_id in ids.items():
        wait_status(email_id, expected[name], timeout=120)
    # Cards wait for a click; WF2 runs that are done are 'success', the card holders 'waiting'.
    wait_until(lambda: all(len([x for x in executions("wf2", i, since) if x["status"] in ("success", "waiting")]) == 3
                           for i in ids.values()), 120, what="all WF2 runs to settle")
    for name in ("quote", "booking"):
        buttons = wait_card(ids[name])
        status = click(buttons["Approve and send" if name == "quote" else "Reject"])
        r.check(status == 200, f"{name}: clicked {'Approve' if name == 'quote' else 'Reject'} -> HTTP {status}")
    wait_status(ids["quote"], "done")
    wait_status(ids["booking"], "rejected")
    quiet()
    state = mock_state()
    for name, email_id in ids.items():
        runs = executions("wf2", email_id, since)
        statuses = sorted(x["status"] for x in runs)
        e = email(email_id)
        r.check(all(s in ("success", "waiting") for s in statuses) and len(runs) == 3,
                f"{name} #{email_id}: status {e['status']}, WF2 runs {statuses}")
        r.check(len(rows("draft", email_id)) == (0 if name in ("other", "injection") else 1),
                f"{name}: {len(rows('draft', email_id))} draft row(s)")
    keys = {name: actions_by_key(i) for name, i in ids.items()}
    q, b = ids["quote"], ids["booking"]
    r.check(len(state["deals"]) - len(before["deals"]) == 1 and f"hubspot_deal:{q}" in keys["quote"],
            f"quote: one HubSpot deal ({len(state['deals']) - len(before['deals'])} created)")
    r.check(len(state["notes"]) - len(before["notes"]) == 1, f"quote: one note ({len(state['notes'])})")
    r.check(len(state["tasks"]) - len(before["tasks"]) == 1 and f"hubspot_task:{b}" in keys["booking"],
            f"booking: one task ({len(state['tasks'])})")
    lookups = [k for k in keys["status"] if k.startswith("shipmatch_lookup:")]
    r.check(len(lookups) >= 1, f"status: lookup logged ({len(lookups)} lookup rows, one per run that looked up)")
    uploads = [a for k, a in keys["paperwork"].items() if k.startswith("shipmatch_upload:")]
    r.check(len(state["documents"]) == 2 and len(uploads) == 2 and all(a["ok"] for a in uploads),
            f"paperwork: 2 documents in ShipMatch {[d['filename'] for d in state['documents']]}, .exe skipped")
    r.check(len(slack_for(ids["claim"], state, "#ops-urgent")) == 1, "claim: one #ops-urgent alert")
    for name in ("other", "injection"):
        r.check(len(slack_for(ids[name], state, "#ops-review")) == 1, f"{name}: one #ops-review alert")
    for name in ("quote", "booking", "status", "paperwork", "claim"):
        r.check(len(slack_for(ids[name], state, "#ops-approvals")) == 1, f"{name}: one approval card")
    sent = sent_for(q, state)
    r.check(len(sent) == 1 and "sarah@acme.example" in sent[0]["to"], f"quote approved: {len(sent)} Gmail send(s) to {[s['to'] for s in sent]}")
    r.check(state["labels"].get(email(q)["gmail_message_id"]) == ["Label_processed"], "quote: labelled ai-processed")
    r.check(not sent_for(b, state) and state["labels"].get(email(b)["gmail_message_id"]) == ["Label_rejected"],
            "booking rejected: nothing sent, labelled ai-rejected")
    r.check(not sql("inbox", f"select id from inbox_failure where email_id in ({','.join(map(str, ids.values()))})"),
            "no Failure rows")
    r.ids = ids


def late_rerun(r, email_id, label, since, last="Already processed"):
    """Run WF2 again on an email WF2 is already done with: it must end quietly, unchanged."""
    before = status_of(email_id)
    run_wf2(email_id)
    quiet()
    x = executions("wf2", email_id, since)[-1]
    r.check(x["status"] == "success" and x["last"] == last and status_of(email_id) == before
            and not rows("failure", email_id), f"{label}: rerun on a {before} email -> {short(x)}, still {status_of(email_id)}")


def s_rerun_after_draft(r):
    """1b. A WF2 run that arrives after the email already has its approval card (or was decided)."""
    mock("/_reset", {})
    since = last_execution_id()
    email_id = quote_email("late-rerun")
    run_wf2(email_id)
    buttons = wait_card(email_id)
    r.note(f"Approve link: {buttons['Approve and send'][:60]}...")
    late_rerun(r, email_id, "awaiting approval", since)
    r.check(len(slack_for(email_id, None, "#ops-approvals")) == 1, "still one approval card")
    click(buttons["Reject"])
    wait_status(email_id, "rejected")
    quiet()
    late_rerun(r, email_id, "rejected", since)


def paperwork(name, *files, subject=None):
    return new_email(name, subject=subject or f"Invoice {name}", text="Please find attached invoice INV-77 and the bill of lading.",
                     attachments=[attachment(f) for f in files])


def s_refusal(r):
    """3. #2/#5/#7/#8: ShipMatch refuses with 402 and 400 -> review with readable reason, no draft; rerun same."""
    mock("/_reset", {})
    since = last_execution_id()
    email_id = paperwork("refusal", "INV_paused402.pdf", "BL_refuse400.pdf")
    run_wf2(email_id)
    wait_status(email_id, "needs_review")
    quiet()
    e = email(email_id)
    reason = e["needs_review_reason"] or ""
    r.note(f"reason: {reason}")
    r.check(reason.startswith("ShipMatch did not accept: ") and "INV_paused402.pdf: uploads paused" in reason
            and "BL_refuse400.pdf: file rejected" in reason and "att-" not in reason and "HTTP None" not in reason,
            "readable refusal reason (filenames, no raw attachment id)")
    drafts = rows("draft", email_id)
    r.check(len(drafts) == 1 and not drafts[0]["ok"] and drafts[0]["body"] == "", f"one blocked draft, no AI body ({len(drafts)})")
    r.check(mock_state()["draft_calls"] == 0, "no drafting LLM call")
    first = executions("wf2", email_id, since)
    r.check(len(first) == 1 and first[0]["status"] == "success" and first[0]["nodes"].get("Draft (Django)") == 1,
            f"WF2 {short(first[0])}, /draft called {first[0]['nodes'].get('Draft (Django)')}x")
    review = slack_for(email_id, None, "#ops-review")
    r.check(len(review) == 1 and "ShipMatch did not accept" in review[0]["text"], "one #ops-review alert with the reason")
    # #7: a rerun gets the same answer and doesn't alert again.
    run_wf2(email_id)
    quiet()
    second = executions("wf2", email_id, since)[-1]
    answer = node_json(second, "Draft (Django)")
    r.check(second["status"] == "success" and answer and answer[0].get("ok") is False and answer[0].get("rerun") is True,
            f"rerun: {short(second)}, /draft answered ok={answer[0].get('ok') if answer else None} rerun={answer[0].get('rerun') if answer else None}")
    r.check(status_of(email_id) == "needs_review" and email(email_id)["needs_review_reason"] == reason, "rerun: same status and reason")
    r.check(len(rows("draft", email_id)) == 1, "rerun: no new draft")
    r.check(len(slack_for(email_id, None, "#ops-review")) == 1, "rerun: review alert still sent once")
    r.check(not rows("failure", email_id), "no Failure rows")


def s_transient_retry(r):
    """4 + 2. #5: 503 upload -> failure + alert + failed; Retry repeats only that upload (#3: one /draft)."""
    mock("/_reset", {})
    faults({"id": "flaky", "target": "upload", "match": "B_flaky.pdf", "status": 503})
    since = last_execution_id()
    email_id = paperwork("transient", "A_good.pdf", "B_flaky.pdf")
    run_wf2(email_id)
    wait_status(email_id, "failed")
    quiet()
    run1 = executions("wf2", email_id, since)
    r.check(len(run1) == 1 and run1[0]["status"] == "error", f"WF2 {short(run1[0])}")
    failures = rows("failure", email_id)
    r.check(len(failures) == 1 and "B_flaky.pdf" in failures[0]["error"] and not failures[0]["resolved"],
            f"Failure recorded: {failures[0]['workflow']} / {failures[0]['node']}: {failures[0]['error'][:100]}" if failures else "Failure recorded")
    alerts = slack_for(email_id, None, "#ops-alerts")
    r.check(len(alerts) == 1 and "ShipMatch upload failed" in alerts[0]["text"], f"{len(alerts)} #ops-alerts message")
    keys = actions_by_key(email_id)
    good = next(a for k, a in keys.items() if k.endswith(":att-A_good.pdf"))
    flaky = next(a for k, a in keys.items() if k.endswith(":att-B_flaky.pdf"))
    r.check(good["ok"] and flaky["response"].get("released"), f"A logged ok; B claim released ({flaky['response']})")
    r.check(not rows("draft", email_id), "no draft yet")
    e = email(email_id)
    r.note(f"failed reason: {e['needs_review_reason'][:150]}")
    # Retry from the dashboard after ShipMatch is back.
    clear_faults()
    uploads_before = len(mock_state()["uploads"])
    since2 = last_execution_id()
    page = dashboard().post(f"/emails/{email_id}/retry/", {"next": f"/emails/{email_id}/"})
    r.note(f"Retry flash: {Dashboard.flashes(page)}")
    wait_status(email_id, "awaiting_approval", timeout=120)
    quiet()
    state = mock_state()
    run2 = executions("wf2", email_id, since2)
    r.check(len(run2) == 1 and run2[0]["status"] == "waiting", f"retry WF2 {short(run2[0])}" if run2 else "retry WF2 ran")
    retried = [u["filename"] for u in state["uploads"][uploads_before:]]
    r.check(retried == ["B_flaky.pdf"], f"retry uploaded only {retried}")
    # #3: one /draft call in the retry run, after the upload was logged.
    x = run2[0]
    log_start = x["run_data"]["Log upload"][-1]["startTime"] if "Log upload" in x["run_data"] else None
    draft_start = x["run_data"]["Draft (Django)"][0]["startTime"] if "Draft (Django)" in x["run_data"] else None
    r.check(x["nodes"].get("Draft (Django)") == 1 and x["nodes"].get("Ready for draft") == 1,
            f"#3 mixed done/new uploads: Ready for draft x{x['nodes'].get('Ready for draft')}, /draft x{x['nodes'].get('Draft (Django)')}")
    r.check(log_start and draft_start and draft_start > log_start, "#3 /draft ran after 'Log upload'")
    drafts = rows("draft", email_id)
    r.check(len(drafts) == 1 and drafts[0]["ok"], "one ok draft")
    failures = rows("failure", email_id)
    r.check(failures and all(f["resolved"] for f in failures), "failure marked resolved by Retry")
    r.check(len(slack_for(email_id, state, "#ops-approvals")) == 1, "approval card posted")
    r.email_id = email_id


def outage_then_retry(r, label, email_id, since, node, *, llm_calls_on_retry):
    """The Anthropic outage rule is in place: WF2 must fail at `node` (no draft, no review),
    and a dashboard Retry after the outage must finish the email."""
    wait_status(email_id, "failed", "needs_review", "awaiting_approval", timeout=150)
    quiet()
    x = executions("wf2", email_id, since)[-1]
    answer = node_json(x, node.replace(" failed", " (Django)"))
    r.note(f"({label}) Django answered {json.dumps(answer[0])[:220] if answer else None}")
    r.check(status_of(email_id) == "failed" and x["status"] == "error" and x["last"] == node
            and "AI service unavailable" in (x["error"] or ""), f"({label}) WF2 {short(x)}, email {status_of(email_id)}")
    r.check(answer and answer[0].get("retry") is True and answer[0].get("ok") is False, f"({label}) 503 retry answer")
    r.check(not rows("draft", email_id) and not email(email_id)["needed_review_at"], f"({label}) no blocked draft, never in review")
    failures = rows("failure", email_id)
    r.check(len(failures) == 1 and failures[0]["node"] == node, f"({label}) Failure at {failures[0]['node'] if failures else None}")
    r.check(len(slack_for(email_id, None, "#ops-alerts")) == 1 and not slack_for(email_id, None, "#ops-review"),
            f"({label}) one #ops-alerts message, no review alert")
    clear_faults()
    calls = mock_state()["llm_calls"]
    dashboard().post(f"/emails/{email_id}/retry/", {"next": f"/emails/{email_id}/"})
    wait_status(email_id, "awaiting_approval", timeout=120)
    quiet()
    r.check(mock_state()["llm_calls"] - calls == llm_calls_on_retry,
            f"({label}) Retry: {mock_state()['llm_calls'] - calls} LLM call(s) (expected {llm_calls_on_retry})")
    r.check(len(rows("draft", email_id)) == 1 and rows("draft", email_id)[0]["ok"]
            and len(slack_for(email_id, None, "#ops-approvals")) == 1 and all(f["resolved"] for f in rows("failure", email_id)),
            f"({label}) after Retry: ok draft, approval card, failure resolved")


def s_draft_errors(r):
    """5. #14 + observation 1: Anthropic outage while drafting or triaging -> failed + Retry;
    a malformed Anthropic answer that makes /draft 500."""
    mock("/_reset", {})
    since = last_execution_id()
    # (a) Anthropic answers 500 while drafting (the SDK retries twice): /draft 503, WF2 fails, Retry.
    a = quote_email("llm500", subject="rate pls llm500")
    faults({"target": "anthropic", "kind": "draft", "match": "llm500", "status": 500,
            "body": {"type": "error", "error": {"type": "api_error", "message": "mock outage"}}})
    run_wf2(a)
    outage_then_retry(r, "a: draft 500", a, since, "Draft failed", llm_calls_on_retry=1)
    # (c) Anthropic answers 529 (overloaded) while classifying: /triage 503, nothing saved, Retry triages.
    c = quote_email("llmtri", subject="rate pls llmtri")
    faults({"target": "anthropic", "kind": "classify", "match": "llmtri", "status": 529,
            "body": {"type": "error", "error": {"type": "overloaded_error", "message": "mock overload"}}})
    run_wf2(c)
    outage_then_retry(r, "c: classify 529", c, since, "Triage failed", llm_calls_on_retry=3)
    r.check(len(rows("triage", c)) == 1, "(c) one Triage row, saved by the retry")
    # (b) Anthropic answers 200 with a malformed message: /draft itself fails with 500.
    b = quote_email("llmbad", subject="rate pls llmbad")
    faults({"id": "bad", "target": "anthropic", "kind": "draft", "match": "llmbad", "malformed": True})
    run_wf2(b)
    wait_status(b, "failed", timeout=120)
    quiet()
    x = executions("wf2", b, since)[-1]
    answer = node_json(x, "Draft (Django)")
    r.note(f"(b) malformed -> Django /draft answered {json.dumps(answer[0])[:160] if answer else None}")
    r.check(x["status"] == "error" and x["last"] == "Draft failed", f"(b) WF2 stops with an error: {short(x)}")
    failures = rows("failure", b)
    r.check(len(failures) == 1 and failures[0]["node"] == "Draft failed", f"(b) Failure at {failures[0]['node'] if failures else None}")
    r.check(len(slack_for(b, None, "#ops-alerts")) == 1, "(b) #ops-alerts message")
    clear_faults()
    calls = mock_state()["llm_calls"]
    dashboard().post(f"/emails/{b}/retry/", {"next": f"/emails/{b}/"})
    wait_status(b, "awaiting_approval", timeout=120)
    quiet()
    r.check(mock_state()["llm_calls"] - calls == 1, f"(b) Retry reused the triage: {mock_state()['llm_calls'] - calls} LLM call (the draft)")
    r.check(rows("draft", b)[-1]["ok"] and len(slack_for(b, None, "#ops-approvals")) == 1, "(b) after Retry: ok draft and approval card")


def s_approval_errors(r):
    """6. #15: /approval answers 422 -> WF3 error + Failure; a genuinely decided email -> quiet."""
    mock("/_reset", {})
    since = last_execution_id()
    # (a) A sender that couldn't be parsed: Django refuses to approve a reply (422).
    a = new_email("nosender", subject="rate pls nosender", text=QUOTE["text"], sender="Sarah Khan <not-an-address>")
    run_wf2(a)
    wait_status(a, "needs_review")
    quiet()
    r.check(email(a)["from_email"] == "", f"(a) intake kept no address; status {status_of(a)}")
    run_wf3(a, "approved", final_body="Hi Sarah, thanks. Ops Team")
    wait_status(a, "failed")
    quiet()
    x = executions("wf3", a, since)[-1]
    answer = node_json(x, "Record decision (Django)")
    r.note(f"(a) /approval answered {answer[0] if answer else None}")
    r.check(x["status"] == "error" and x["last"] == "Decision not recorded", f"(a) WF3 {short(x)}")
    failures = rows("failure", a)
    r.check(len(failures) == 1 and "no valid sender address" in failures[0]["error"], "(a) Failure recorded with Django's reason")
    r.check(not sent_for(a), "(a) nothing sent")
    # A WF2 run on a failed email (not through Retry) is an unexpected /triage answer: error, not silence.
    run_wf2(a)
    quiet()
    x = executions("wf2", a, since)[-1]
    r.check(x["status"] == "error" and x["last"] == "Triage failed" and "failed" in (x["error"] or ""),
            f"(a) WF2 on the failed email: {short(x)}")
    # (b) Rejected in the dashboard, then the Slack Approve link is clicked: already decided, quiet.
    b = quote_email("decided")
    run_wf2(b)
    buttons = wait_card(b)
    page = dashboard().post(f"/emails/{b}/decide/", {"action": "reject", "final_body": ""})
    r.note(f"(b) dashboard reject: {Dashboard.flashes(page)}")
    wait_status(b, "rejected")
    status = click(buttons["Approve and send"])
    r.check(status == 200, f"(b) old Slack Approve clicked -> HTTP {status}")
    quiet()
    x = executions("wf3", b, since)[-1]
    r.check(x["status"] == "success" and x["last"] == "Already decided otherwise", f"(b) WF3 {short(x)}")
    r.check(status_of(b) == "rejected" and not sent_for(b) and not rows("failure", b), "(b) still rejected, nothing sent, no Failure")


def s_stale(r):
    """7. #16: re-draft in the dashboard, then the OLD Slack Approve link -> nothing sent, review."""
    mock("/_reset", {})
    since = last_execution_id()
    email_id = quote_email("stale")
    run_wf2(email_id)
    buttons = wait_card(email_id)
    old = rows("draft", email_id)[-1]["id"]
    page = dashboard().post(f"/emails/{email_id}/redraft/", {})
    r.note(f"re-draft: {Dashboard.flashes(page)}")
    drafts = rows("draft", email_id)
    r.check(len(drafts) == 2 and drafts[-1]["ok"], f"new draft #{drafts[-1]['id']} (card showed #{old})")
    status = click(buttons["Approve and send"])
    r.check(status == 200, f"old Approve clicked -> HTTP {status}")
    wait_status(email_id, "needs_review")
    quiet()
    x = executions("wf3", email_id, since)[-1]
    answer = node_json(x, "Record decision (Django)")
    r.check(x["status"] == "success" and x["last"] == "Already decided otherwise" and answer and answer[0].get("stale_draft"),
            f"WF3 {short(x)} (stale_draft={answer[0].get('stale_draft') if answer else None})")
    r.check(not sent_for(email_id) and not rows("approval", email_id), "nothing sent, no Approval saved")
    r.check("older draft" in (email(email_id)["needs_review_reason"] or ""), f"reason: {email(email_id)['needs_review_reason'][:80]}")
    late_rerun(r, email_id, "in review with an ok draft", since, last="Left to the reviewer")
    body = drafts[-1]["body"]
    page = dashboard().post(f"/emails/{email_id}/decide/", {"action": "approve", "final_body": body})
    r.note(f"dashboard approve: {Dashboard.flashes(page)}")
    wait_status(email_id, "done")
    quiet()
    sent = sent_for(email_id)
    r.check(len(sent) == 1 and sent[0]["body"].strip() == body.strip(), f"{len(sent)} reply sent, with the new draft")
    approvals = rows("approval", email_id)
    r.check(len(approvals) == 1 and approvals[0]["channel"] == "dashboard" and approvals[0]["decision"] == "approved",
            f"approval: {[(a['channel'], a['decision'], a['reviewer']) for a in approvals]}")
    late_rerun(r, email_id, "done", since)
    r.check(len(sent_for(email_id)) == 1, "still one reply after the rerun")


def s_send_uncertain(r):
    """8. #18: Gmail send fails after the reply_sent claim -> no automatic resend; 'Send again anyway' once."""
    mock("/_reset", {})
    since = last_execution_id()
    email_id = quote_email("sendfail")
    run_wf2(email_id)
    buttons = wait_card(email_id)
    faults({"id": "gmail", "target": "gmail_send", "status": 503,
            "body": {"error": {"code": 503, "message": "Backend Error", "status": "UNAVAILABLE"}}})
    click(buttons["Approve and send"])
    wait_status(email_id, "failed")
    quiet()
    x = executions("wf3", email_id, since)[-1]
    r.check(x["status"] == "error" and x["last"] == "Gmail send reply", f"WF3 {short(x)}")
    e = email(email_id)
    r.check("reply may already have been sent" in (e["needs_review_reason"] or ""), f"reason: {e['needs_review_reason'][:110]}")
    reply = actions_by_key(email_id).get(f"reply_sent:{email_id}")
    r.check(reply and reply["response"].get("uncertain") and not reply["ok"], f"reply_sent claim: {reply and reply['response']}")
    r.check(len(slack_for(email_id, None, "#ops-alerts")) == 1, "#ops-alerts message")
    since2 = last_execution_id()
    page = dashboard().post(f"/emails/{email_id}/retry/", {"next": f"/emails/{email_id}/"})
    flashes = Dashboard.flashes(page)
    r.check(any("Not retried" in f for f in flashes) and status_of(email_id) == "failed", f"Retry refused: {flashes}")
    time.sleep(3)
    r.check(not executions("wf3", email_id, since2), "Retry started no WF3 run")
    detail = dashboard().get(f"/emails/{email_id}/")
    r.check("Mark as sent" in detail and "Send again anyway" in detail and ">Retry</button>" not in detail,
            "email page offers Mark as sent / Send again anyway, not Retry")
    clear_faults()
    page = dashboard().post(f"/emails/{email_id}/reply/", {"action": "resend"})
    r.note(f"send again: {Dashboard.flashes(page)}")
    wait_status(email_id, "done")
    quiet()
    sent = sent_for(email_id)
    r.check(len(sent) == 1, f"{len(sent)} reply sent after 'Send again anyway'")
    x = executions("wf3", email_id, since2)
    r.check(len(x) == 1 and x[0]["status"] == "success", f"WF3 {short(x[0])}" if x else "WF3 ran")


def s_timeout(r):
    """9. Approval timeout -> needs_review + alert; decided meanwhile -> ends quietly."""
    mock("/_reset", {})
    minutes = int(ENV.get("APPROVAL_TIMEOUT_MINUTES", "1440"))
    if minutes > 2:
        r.check(False, f"APPROVAL_TIMEOUT_MINUTES is {minutes}; set it to 1 to run this scenario")
        return
    since = last_execution_id()
    idle = quote_email("timeout")
    decided = quote_email("meanwhile")
    run_wf2(idle)
    run_wf2(decided)
    wait_card(idle)
    wait_card(decided)
    page = dashboard().post(f"/emails/{decided}/decide/", {"action": "approve", "final_body": rows("draft", decided)[-1]["body"]})
    r.note(f"dashboard approve meanwhile: {Dashboard.flashes(page)}")
    wait_status(decided, "done")
    wait_status(idle, "needs_review", timeout=minutes * 60 + 90)
    wait_until(lambda: all(x["status"] != "waiting" for x in executions("wf2", None, since)
                           if x["email_id"] in (idle, decided)), minutes * 60 + 90, what="waiting runs to time out")
    quiet()
    x = executions("wf2", idle, since)[-1]
    r.check(x["status"] == "success" and "Approval timed out" in x["nodes"] and x["nodes"].get("Slack #ops-review") == 1,
            f"timeout: WF2 {short(x)}")
    r.check("approval timeout" in (email(idle)["needs_review_reason"] or ""), f"reason: {email(idle)['needs_review_reason']}")
    r.check(len(slack_for(idle, None, "#ops-review")) == 1, "one #ops-review alert")
    y = executions("wf2", decided, since)[-1]
    r.check(y["status"] == "success" and y["last"] == "Already decided", f"decided meanwhile: WF2 {short(y)}")
    r.check(status_of(decided) == "done" and len(sent_for(decided)) == 1 and not slack_for(decided, None, "#ops-review"),
            "decided email stays done, one reply, no review alert")


def s_race(r):
    """#2: two WF2 runs on the same paperwork, the first upload slow and refused -> never a 'received' draft."""
    mock("/_reset", {})
    faults({"target": "upload", "match": "C_slow_paused402.pdf", "delay": 8})
    since = last_execution_id()
    email_id = paperwork("race", "C_slow_paused402.pdf", "D_ok.pdf")
    run_wf2(email_id)
    wait_until(lambda: any(k.startswith("shipmatch_upload:") for k in actions_by_key(email_id)), 60, what="first upload claim")
    time.sleep(1)
    run_wf2(email_id)
    wait_status(email_id, "needs_review", "awaiting_approval", timeout=120)
    quiet()
    runs = executions("wf2", email_id, since)
    for x in runs:
        r.note(f"WF2 {short(x)}; /draft answers {[ {k: a.get(k) for k in ('ok', 'pending', 'rerun')} for a in node_json(x, 'Draft (Django)')]}")
    r.check(len(runs) == 2 and all(x["status"] == "success" for x in runs), "both runs succeed")
    r.check(any(x["last"] == "Another run drafts" for x in runs), "the second run stopped quietly at 'Another run drafts'")
    drafts = rows("draft", email_id)
    r.check(status_of(email_id) == "needs_review" and len(drafts) == 1 and not drafts[0]["ok"],
            f"email {status_of(email_id)}, drafts {[d['ok'] for d in drafts]} (no ok draft)")
    r.check(len(slack_for(email_id, None, "#ops-review")) == 1 and not slack_for(email_id, None, "#ops-approvals"),
            "one review alert, no approval card")


def s_resumed_failure(r):
    """Observation 3: a resumed approval run fails before calling Django again, after an
    overlapping run touched the email -> WF4 still attaches the Failure to the email."""
    mock("/_reset", {})
    since = last_execution_id()
    email_id = quote_email("resumed")
    run_wf2(email_id)
    buttons = wait_card(email_id)
    holder = wait_until(lambda: [x for x in executions("wf2", email_id, since) if x["status"] == "waiting"], 60,
                        what="the card's waiting WF2 run")[0]
    late_rerun(r, email_id, "overlapping run", since)
    last = email(email_id)["last_execution_id"]
    r.check(last != str(holder["id"]), f"email's last execution is now #{last}, not the card's run #{holder['id']}")
    # Make the resumed run fail right after the Wait, before any Django call, exactly like bug A:
    # in this waiting execution's own copy of WF2, "Send reply (WF3)" passes draft_id as a number
    # again, so n8n refuses the sub-workflow call ("Invalid input for 'draft_id'").
    fixed, buggy = "String($('Draft (Django)').item.json.draft_id)", "$('Draft (Django)').item.json.draft_id"
    dc("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "n8n", "-c",
       f"""update execution_data set "workflowData" = replace("workflowData"::text, '{fixed.replace("'", "''")}',
           '{buggy.replace("'", "''")}')::json where "executionId" = {int(holder['id'])}""")
    status = click(buttons["Approve and send"])
    r.check(status == 200, f"Approve clicked -> HTTP {status}")
    wait_until(lambda: next(x for x in executions("wf2", email_id, since) if x["id"] == holder["id"])["status"] != "waiting",
               90, what="the resumed run to finish")
    quiet()
    x = next(x for x in executions("wf2", email_id, since) if x["id"] == holder["id"])
    r.check(x["status"] == "error" and x["last"] == "Send reply (WF3)", f"resumed run {short(x)}")
    failures = rows("failure", email_id)
    r.check(len(failures) == 1 and failures[0]["execution_id"] == str(holder["id"]),
            f"Failure attached to the email: {[(f['execution_id'], f['node']) for f in failures]}")
    r.check(status_of(email_id) == "failed" and not sent_for(email_id) and not rows("approval", email_id),
            f"email {status_of(email_id)}, nothing sent, no Approval")
    alerts = slack_for(email_id, None, "#ops-alerts")
    r.check(len(alerts) == 1, f"{len(alerts)} #ops-alerts message linking the email")
    unattached = sql("inbox", f"select id from inbox_failure where email_id is null and execution_id = '{holder['id']}'")
    r.check(not unattached, "no unattached Failure for that execution")
    # Retry starts over from WF2 (no decision was recorded) and a new card is approved.
    since2 = last_execution_id()
    dashboard().post(f"/emails/{email_id}/retry/", {"next": f"/emails/{email_id}/"})
    wait_status(email_id, "awaiting_approval", timeout=120)
    new_card = wait_until(lambda: [m for m in slack_for(email_id, None, "#ops-approvals") if m.get("blocks")][1:], 90,
                          what="a second approval card")
    click({b["text"]["text"]: b["url"] for b in new_card[-1]["blocks"][1]["elements"]}["Approve and send"])
    wait_status(email_id, "done")
    quiet()
    r.check(len(sent_for(email_id)) == 1 and all(f["resolved"] for f in rows("failure", email_id)),
            f"after Retry and Approve: {len(sent_for(email_id))} reply, failure resolved")
    r.check(all(x["status"] in ("success", "waiting") for x in executions("wf2", email_id, since2)), "retry runs succeed")


def s_metrics(r):
    """10. #17: /internal/metrics?period=today and the WF5 digest agree with the database."""
    mock("/_reset", {})
    status, m = request(f"{APP}/internal/metrics?period=today", headers={"X-Internal-Token": ENV["INTERNAL_TOKEN"]})
    r.check(status == 200, f"metrics HTTP {status}")
    since = m["since"]
    db = sql("inbox", f"""select
        (select count(*) from inbox_email where received_at >= '{since}') as handled,
        (select count(*) from inbox_action where kind = 'reply_sent' and ok and created_at >= '{since}') as replies,
        (select count(*) from inbox_failure where created_at >= '{since}') as failures,
        (select count(*) from inbox_failure where not resolved) as open_failures,
        (select count(*) from inbox_email where status = 'needs_review') as needs_review,
        (select count(*) from inbox_email where status = 'awaiting_approval') as awaiting,
        (select count(*) from inbox_email where status = 'rejected' and received_at >= '{since}') as rejected""")[0]
    for key, mkey in (("handled", "handled"), ("replies", "replies_sent"), ("failures", "failures"),
                      ("open_failures", "open_failures"), ("needs_review", "needs_review_now"),
                      ("awaiting", "awaiting_approval_now"), ("rejected", "rejected")):
        r.check(db[key] == m[mkey], f"{mkey}: metrics {m[mkey]} vs database {db[key]}")
    # #17: an email that failed and was then sent unchanged counts as automatic.
    retried_sent = sql("inbox", f"""select distinct e.id from inbox_email e join inbox_failure f on f.email_id = e.id
        join inbox_action a on a.email_id = e.id and a.kind = 'reply_sent' and a.ok
        where e.received_at >= '{since}' and not exists (select 1 from inbox_approval p where p.email_id = e.id and p.decision = 'edited')
        and e.needed_review_at is null
        and not exists (select 1 from inbox_triage t where t.email_id = e.id and t.model_classify like 'human:%')
        and not exists (select 1 from inbox_draft d where d.email_id = e.id and not d.ok)""")
    r.note(f"replies {m['replies_sent']}: automatic {m['automatic']}, reviewed {m['reviewed']}; failed-then-sent-unchanged: {[x['id'] for x in retried_sent]}")
    r.check(m["automatic"] >= len(retried_sent) and m["automatic"] <= m["replies_sent"], "automatic count includes failed-then-retried replies")
    # Observation 2: the same definition from the database (Email.needed_review_at, not the review-alert key).
    human = """(e.needed_review_at is not null
        or exists (select 1 from inbox_triage t where t.email_id = e.id and t.model_classify like 'human:%')
        or exists (select 1 from inbox_draft d where d.email_id = e.id and not d.ok)
        or exists (select 1 from inbox_approval p where p.email_id = e.id and p.decision = 'edited'))"""
    db2 = sql("inbox", f"""select
        (select count(*) from inbox_action a join inbox_email e on e.id = a.email_id
          where a.kind = 'reply_sent' and a.ok and a.created_at >= '{since}' and not {human}) as automatic,
        (select count(*) from inbox_email e where (e.received_at >= '{since}' or exists (select 1 from inbox_action a
          where a.email_id = e.id and a.kind = 'reply_sent' and a.ok and a.created_at >= '{since}')) and {human}) as reviewed""")[0]
    r.check(db2["automatic"] == m["automatic"] and db2["reviewed"] == m["reviewed"],
            f"automatic/reviewed: metrics {m['automatic']}/{m['reviewed']} vs database {db2['automatic']}/{db2['reviewed']}")
    stale = sql("inbox", f"""select e.id, e.status, e.needed_review_at from inbox_email e
        where e.gmail_message_id = 'e2e-{STAMP}-stale' and e.received_at >= '{since}'""")
    if stale:
        s = stale[0]
        alerts = sql("inbox", f"select id from inbox_action where idempotency_key = 'slack_alert:{s['id']}:review'")
        r.check(s["status"] == "done" and s["needed_review_at"] and not alerts,
                f"stale scenario email #{s['id']}: {s['status']}, needed_review_at set, no review alert -> counted as reviewed")
    else:
        r.note("stale scenario not run in this session; its check is skipped")
    # WF5 "Run now".
    since_exec = last_execution_id()
    dc("exec", "-T", "n8n", "n8n", "execute", "--id=" + WF["wf5"])
    digest = wait_until(lambda: [s for s in mock_state()["slack"] if "AI Ops Inbox" in (s.get("text") or "")], 120,
                        what="digest message")
    r.note("digest:\n        " + digest[-1]["text"].replace("\n", "\n        "))
    r.check(digest[-1]["text"] == m["digest_text"], "WF5 digest text matches /internal/metrics digest_text")
    x = executions("wf5", None, since_exec)
    r.check(x and x[-1]["status"] == "success", f"WF5 {short(x[-1])}" if x else "WF5 ran")


LANE_TEXT = ("Hi,\nPlease quote for the shipment below.\n\nFrom: Lahore\nTo: Karachi\nWeight: 1200 kg\n"
             "Pieces: 4 pallets\nPickup date: 25 Oct\n\nThanks")
OUTLOOK_TEXT = ("Thanks, see you then.\n\nFrom: Ops Desk <ops@carrier.example>\nSent: Monday, 12 October 2026 10:00\n"
                "To: Sarah Khan <sarah@acme.example>\nSubject: RE: rate\n\nWe can move 4 pallets Lahore to Karachi.")


def s_cleaning(r):
    """#11: a quote laid out as "From: Lahore / To: Karachi / ..." keeps its lane; a real Outlook
    quoted reply is still cut at its header block."""
    mock("/_reset", {})
    lane = new_email("lane", subject="rate pls lane", text=LANE_TEXT)
    outlook = new_email("outlook", subject="Re: rate", text=OUTLOOK_TEXT)
    e = email(lane)
    r.check("From: Lahore" in e["body_clean"] and "Pickup date: 25 Oct" in e["body_clean"],
            f"lane email: cleaned body keeps the lane block ({len(e['body_clean'])} of {len(e['body_text'])} chars)")
    o = email(outlook)
    r.check(o["body_clean"].strip() == "Thanks, see you then.", f"Outlook reply cut at its header: {o['body_clean']!r}")
    run_wf2(lane)
    wait_status(lane, "awaiting_approval", "needs_review", "failed", timeout=120)
    quiet()
    t = rows("triage", lane)[-1]
    r.note(f"triage: {t['category']} route {t['route']}, fields {json.dumps(t['fields'])[:200]}, missing {t['missing_fields']}")
    r.check(t["category"] == "quote_request", f"category {t['category']}")
    fields = json.dumps(t["fields"])
    r.check("Lahore" in fields and "Karachi" in fields, "origin Lahore and destination Karachi extracted")
    asked = " ".join(t["missing_fields"]).lower()
    r.check("origin" not in asked and "destination" not in asked, f"lane not in missing_fields {t['missing_fields']}")
    d = rows("draft", lane)
    r.check(d and d[-1]["ok"] and "origin" not in d[-1]["body"].lower() and "destination" not in d[-1]["body"].lower(),
            "draft does not ask for origin/destination")
    if d:
        r.note("draft: " + d[-1]["body"].replace("\n", " | ")[:300])
    r.check(status_of(lane) == "awaiting_approval" and approval_buttons(lane), f"lane email {status_of(lane)}, approval card")
    finish(r, [lane])


def finish(r, ids):
    """Approve the scenario's open cards so no email times out into review during a later scenario."""
    for email_id in ids:
        if approval_buttons(email_id):
            click(approval_buttons(email_id)["Approve and send"])
    for email_id in ids:
        wait_status(email_id, "done", "failed", "needs_review")
    quiet()
    r.check(all(status_of(i) == "done" and len(sent_for(i)) == 1 for i in ids), f"approved: {len(ids)} email(s) done, 1 reply each")


def s_naive_date(r):
    """#12: Date headers without a zone (or -0000) are saved as UTC and the email runs normally."""
    mock("/_reset", {})
    cases = [("date_minus0000", "Mon, 12 Oct 2026 10:15:00 -0000", "2026-10-12 10:15:00+00"),
             ("date_nozone", "Mon, 12 Oct 2026 10:15:00", "2026-10-12 10:15:00+00"),
             ("date_iso_naive", "2026-10-12T10:15:00", "2026-10-12 10:15:00+00"),
             ("date_karachi", "Mon, 12 Oct 2026 15:15:00 +0500", "2026-10-12 10:15:00+00")]
    ids = {}
    for name, header, expected in cases:
        try:
            ids[name] = new_email(name, subject=f"rate pls {name}", text=QUOTE["text"], date=header)
        except AssertionError as exc:
            r.check(False, f"{name}: intake {header!r} -> {exc}")
            continue
        got = sql("inbox", f"select received_at at time zone 'UTC' as utc from inbox_email where id = {ids[name]}")[0]["utc"]
        r.check(got.startswith(expected[:19].replace(" ", "T")) or got.startswith(expected[:19]),
                f"{name}: intake 200, {header!r} saved as {got} UTC")
    for name, email_id in ids.items():
        run_wf2(email_id)
    for name, email_id in ids.items():
        wait_status(email_id, "awaiting_approval", "needs_review", "failed", timeout=120)
    quiet()
    for name, email_id in ids.items():
        r.check(status_of(email_id) == "awaiting_approval" and approval_buttons(email_id),
                f"{name}: WF2 -> {status_of(email_id)}, approval card")
    r.check(not sql("inbox", f"select id from inbox_failure where email_id in ({','.join(map(str, ids.values()))})"),
            "no Failure rows")
    finish(r, list(ids.values()))


def s_retry_n8n_down(r):
    """#13: Retry while n8n is down leaves the email failed (and retryable); Retry after it is back works."""
    mock("/_reset", {})
    faults({"id": "down", "target": "anthropic", "kind": "draft", "match": "n8ndown", "status": 500})
    since = last_execution_id()
    email_id = quote_email("n8ndown", subject="rate pls n8ndown")
    run_wf2(email_id)
    wait_status(email_id, "failed", timeout=150)
    quiet()
    clear_faults()
    failures = rows("failure", email_id)
    r.check(len(failures) == 1 and not failures[0]["resolved"], f"email failed with 1 open Failure ({failures[0]['node'] if failures else None})")
    board = dashboard()
    dc("stop", "n8n")
    try:
        try:
            page = board.post(f"/emails/{email_id}/retry/", {"next": f"/emails/{email_id}/"})
            flashes = Dashboard.flashes(page)
            r.check(any("Retry failed" in f and "n8n" in f for f in flashes), f"Retry with n8n stopped -> flash {flashes}")
        except urllib.error.HTTPError as exc:
            r.check(False, f"Retry with n8n stopped -> HTTP {exc.code}")
        e = email(email_id)
        r.check(e["status"] == "failed", f"email still {e['status']} (not stuck in received)")
        r.check(not rows("failure", email_id)[0]["resolved"], "Failure still open")
        page = board.get(f"/emails/{email_id}/")
        r.check(f"/emails/{email_id}/retry/" in page, "email page still offers Retry")
        r.check(f"/emails/{email_id}/" in board.get("/failures/"), "email listed on the failures page")
    finally:
        dc("start", "n8n")
    wait_until(lambda: _webhook_ready(), 180, every=3, what="WF2's webhook after restarting n8n")
    since2 = last_execution_id()
    page = board.post(f"/emails/{email_id}/retry/", {"next": f"/emails/{email_id}/"})
    r.note(f"Retry after n8n is back: {Dashboard.flashes(page)}")
    wait_status(email_id, "awaiting_approval", timeout=150)
    quiet()
    x = executions("wf2", email_id, since2)
    r.check(x and x[-1]["status"] == "waiting", f"retry WF2 {short(x[-1])}" if x else "retry WF2 ran")
    r.check(all(f["resolved"] for f in rows("failure", email_id)), "Failure resolved")
    r.check(len(rows("draft", email_id)) == 1 and approval_buttons(email_id), "ok draft and approval card")
    finish(r, [email_id])


def purge(days=90):
    return dc("exec", "-T", "app", "python", "manage.py", "purge_old_bodies", f"--days={days}").strip()


def s_purge(r):
    """#20: the 90-day purge blanks only closed emails; an old open email keeps its text and
    its approval sends the real reply; a purged draft is never sent."""
    from_mail = "[deleted after retention period]"
    mock("/_reset", {})
    ids = {name: quote_email(f"purge_{name}") for name in ("done", "rejected", "open", "guard")}
    for email_id in ids.values():
        run_wf2(email_id)
    for email_id in ids.values():
        wait_card(email_id, timeout=120)
    quiet()
    r.check(click(approval_buttons(ids["done"])["Approve and send"]) == 200, "approved 'done' in Slack")
    r.check(click(approval_buttons(ids["rejected"])["Reject"]) == 200, "rejected 'rejected' in Slack")
    wait_status(ids["done"], "done")
    wait_status(ids["rejected"], "rejected")
    quiet()
    open_body = rows("draft", ids["open"])[-1]["body"]
    sql_exec("inbox", f"update inbox_email set received_at = now() - interval '100 days' "
                      f"where id in ({','.join(map(str, ids.values()))})")
    out = purge()
    r.note(f"purge_old_bodies: {out}")
    purged = sql("inbox", f"select id from inbox_email where body_text = '{from_mail}' and id in ({','.join(map(str, ids.values()))})")
    r.check(sorted(x["id"] for x in purged) == sorted([ids["done"], ids["rejected"]]),
            "of this scenario's 4 emails, exactly the 2 closed ones were purged")
    for name in ("done", "rejected"):
        e, d, a = email(ids[name]), rows("draft", ids[name]), rows("approval", ids[name])
        r.check(e["body_text"] == from_mail and e["body_clean"] == from_mail and all(x["body"] == from_mail for x in d)
                and all(x["final_body"] == from_mail for x in a) and e["status"] == name,
                f"{name}: body, {len(d)} draft(s), {len(a)} approval(s) purged; status, category, actions kept "
                f"({len(rows('action', ids[name]))} actions)")
    for name in ("open", "guard"):
        e, d = email(ids[name]), rows("draft", ids[name])
        r.check(e["status"] == "awaiting_approval" and e["body_text"] != from_mail and d[-1]["body"] != from_mail,
                f"{name}: still awaiting_approval, body and draft kept")
    r.check(purge().startswith("Purged bodies of 0 emails"), "second purge: nothing left to purge")
    # The old open email: approving it sends the real draft.
    sent_before = len(mock_state()["sent"])
    r.check(click(approval_buttons(ids["open"])["Approve and send"]) == 200, "approved the old open email in Slack")
    wait_status(ids["open"], "done", "failed")
    quiet()
    sent = sent_for(ids["open"])
    r.check(len(sent) == 1 and sent[0]["body"].strip() == open_body.strip() and from_mail not in sent[0]["body"],
            f"open: 1 reply with the real draft text ({len(sent)} sent, status {status_of(ids['open'])})")
    # Guard: a draft that was purged anyway (older code, or a hand edit) must never go out.
    sql_exec("inbox", f"update inbox_draft set body = '{from_mail}' where email_id = {ids['guard']}")
    since = last_execution_id()
    r.check(click(approval_buttons(ids["guard"])["Approve and send"]) == 200, "approved the purged-draft email in Slack")
    wait_status(ids["guard"], "failed", "needs_review", "done")
    quiet()
    r.check(not sent_for(ids["guard"]), f"guard: nothing sent, email {status_of(ids['guard'])}")
    x = executions("wf3", ids["guard"], since)
    r.note("WF3: " + "; ".join(short(e) for e in x))
    f = rows("failure", ids["guard"])
    r.note(f"guard failures: {[(x['node'], x['error'][:120]) for x in f]}; reason: {email(ids['guard'])['needs_review_reason'][:150]}")
    r.check(len(mock_state()["sent"]) - sent_before == 1, "only the open email's reply was sent in this step")


SCENARIOS = {
    "happy": ("1. happy paths, --runs 3, approve/reject", s_happy),
    "late_rerun": ("1b. WF2 rerun after the approval card", s_rerun_after_draft),
    "refusal": ("3. #2/#5/#7/#8 ShipMatch 402/400 refusals", s_refusal),
    "retry": ("4+2. #5/#3 transient 503 upload, Retry, one /draft", s_transient_retry),
    "draft_errors": ("5. #14/obs.1 Anthropic outage while drafting/triaging, malformed answer", s_draft_errors),
    "approval_errors": ("6. #15 /approval 422, already decided", s_approval_errors),
    "stale": ("7. #16 stale Slack approval after re-draft", s_stale),
    "send_uncertain": ("8. #18 Gmail send 503 after claim", s_send_uncertain),
    "race": ("#2 concurrent runs, slow refused upload", s_race),
    "timeout": ("9. approval timeout / decided meanwhile", s_timeout),
    "resumed_failure": ("obs.3 resumed run fails after an overlapping run", s_resumed_failure),
    "cleaning": ("#11 'From: city / To: city' quote keeps its lane; Outlook reply still cut", s_cleaning),
    "naive_date": ("#12 Date headers without a time zone", s_naive_date),
    "retry_n8n_down": ("#13 Retry while n8n is down, then after it is back", s_retry_n8n_down),
    "purge": ("#20 90-day purge: closed only; old open email sends its real reply", s_purge),
    "metrics": ("10. #17/obs.2 metrics and WF5 digest", s_metrics),
}


def _webhook_ready():
    try:
        return request(f"{N8N}/webhook/process", {})[0] == 200
    except OSError:  # connection refused or reset while n8n starts
        return False


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", default=list(SCENARIOS))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for name, (title, _) in SCENARIOS.items():
            print(f"{name:16} {title}")
        return
    # Right after a restart n8n answers 404 until the published webhooks are registered. (A call
    # without the secret starts a WF2 run that stops at "Secret OK?".)
    wait_until(lambda: _webhook_ready(), 180, every=3, what="WF2's webhook to be registered")
    results = []
    for name in args.names:
        title, fn = SCENARIOS[name]
        r = Result(name)
        print(f"\n=== {name}: {title}", flush=True)
        started = time.time()
        try:
            fn(r)
        except Exception as exc:  # a timeout or a broken assumption fails the scenario, not the run
            r.check(False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            try:
                clear_faults()
            except Exception:
                pass
        for line in r.lines:
            print("  " + line, flush=True)
        print(f"  -> {'PASS' if r.ok else 'FAIL'} ({time.time() - started:.0f}s)", flush=True)
        results.append(r)
    print("\nSummary")
    for r in results:
        print(f"  {'PASS' if r.ok else 'FAIL'}  {r.name}")
    sys.exit(0 if all(r.ok for r in results) else 1)


if __name__ == "__main__":
    main()
