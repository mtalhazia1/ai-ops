from datetime import timedelta

from django.utils import timezone

from inbox import metrics, services, state
from inbox.models import Action, Approval, Draft, Email, Failure, Triage

from .conftest import TOKEN
from .test_m1_intake import PAYLOAD
from .test_m4_draft import _awaiting, _triaged


def test_execution_id_is_remembered(api, client, db):
    email_id = api("post", "/emails", PAYLOAD).json()["id"]
    client.get(f"/internal/emails/{email_id}", HTTP_X_INTERNAL_TOKEN=TOKEN, HTTP_X_N8N_EXECUTION_ID="1234")
    assert Email.objects.get(pk=email_id).last_execution_id == "1234"


def test_failure_found_by_execution_id(api, client, fake_llm):
    email_id, _ = _triaged(api, fake_llm)
    client.post(f"/internal/emails/{email_id}/actions/claim", data={"kind": "hubspot_deal", "idempotency_key": "k1"},
                content_type="application/json", HTTP_X_INTERNAL_TOKEN=TOKEN, HTTP_X_N8N_EXECUTION_ID="77")
    body = api("post", "/failures", {"workflow": "WF2 Process", "node": "HubSpot create deal",
                                     "error": "Authorization failed - please check your credentials", "execution_id": "77"}).json()
    assert body["email_id"] == email_id and body["email_status"] == "failed"
    assert ":warning: *WF2 Process* failed at *HubSpot create deal*" in body["alert_text"]
    assert f"/emails/{email_id}/|Open in dashboard>" in body["alert_text"]
    # the claim held by the failed run is released
    assert Action.objects.get(idempotency_key="k1").response.get("released") is True


def test_failure_without_email(api, db):
    body = api("post", "/failures", {"workflow": "WF1 Intake", "node": "Gmail Trigger", "error": "token expired",
                                     "execution_id": "999"}).json()
    assert body["email_id"] is None and "/failures/|Open in dashboard>" in body["alert_text"]
    assert Failure.objects.get().email is None


def _done_email(api, fake_llm, edited=False, received_minutes_ago=30, gid="g"):
    payload = {**PAYLOAD, "gmail_message_id": gid,
               "date": (timezone.now() - timedelta(minutes=received_minutes_ago)).isoformat()}
    fake_llm()
    email_id = api("post", "/emails", payload).json()["id"]
    api("post", f"/emails/{email_id}/triage")
    api("post", f"/emails/{email_id}/draft")
    body = {"decision": "approved"} | ({"final_body": "Edited", "channel": "dashboard"} if edited else {})
    api("post", f"/emails/{email_id}/approval", body)
    api("post", f"/emails/{email_id}/actions", {"kind": "reply_sent", "idempotency_key": f"reply_sent:{email_id}"})
    api("post", f"/emails/{email_id}/status", {"status": "done"})
    return email_id


def test_metrics_for_a_day(api, fake_llm):
    _done_email(api, fake_llm, gid="a", received_minutes_ago=30)
    _done_email(api, fake_llm, edited=True, gid="b", received_minutes_ago=10)
    now = timezone.now().isoformat()  # "today" must not depend on when the suite runs
    review_id, _ = _triaged(api, fake_llm, {**PAYLOAD, "gmail_message_id": "c", "date": now},
                            classification={"category": "other"})
    inj_id, _ = _triaged(api, fake_llm, {**PAYLOAD, "gmail_message_id": "d", "date": now},
                         classification={"contains_instructions_to_ai": True})
    api("post", "/failures", {"workflow": "WF2", "node": "x", "error": "y"})
    m = metrics.compute("today")
    assert m["handled"] == 4
    assert m["replies_sent"] == 2 and m["automatic"] == 1
    assert m["reviewed"] == 3  # two went to review + one edited reply
    assert 9 <= m["avg_minutes_to_reply"] <= 31
    assert m["injection_flagged"] == 1
    assert m["failures"] == 1 and m["open_failures"] == 1
    assert m["needs_review_now"] == 2 and m["awaiting_approval_now"] == 0
    assert m["by_category"]["quote_request"] == 3 and m["by_category"]["other"] == 1
    assert float(m["ai_cost_usd"]) >= 0
    text = metrics.digest_text(m)
    assert "Emails handled: *4*" in text and "fully automatic 1" in text


def test_old_emails_not_counted_today(api, fake_llm):
    _done_email(api, fake_llm, gid="old", received_minutes_ago=60 * 24 * 3)
    assert metrics.compute("today")["handled"] == 0
    assert metrics.compute("7d")["handled"] == 1


def test_metrics_endpoint(api, fake_llm):
    _awaiting(api, fake_llm)
    body = api("get", "/metrics?period=today").json()
    assert body["awaiting_approval_now"] == 1 and "digest_text" in body
    assert api("get", "/metrics?period=year").status_code == 422


def test_metrics_page(admin_client, api, fake_llm):
    _awaiting(api, fake_llm)
    page = admin_client.get("/metrics/?period=7d").content.decode()
    assert "emails handled" in page and "Awaiting approval" in page


def test_triage_injection_counted_once_per_email(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm, classification={"contains_instructions_to_ai": True})
    t = Triage.objects.get(email_id=email_id)
    Triage.objects.create(email_id=email_id, category=t.category, urgency=t.urgency, confidence=1, injection_flag=True)
    assert metrics.compute("today")["injection_flagged"] == 1


def test_ambiguous_execution_is_not_attached(api, client, db):
    for gid in ("x1", "x2"):  # one WF1 execution took in two emails
        client.post("/internal/emails", data={**PAYLOAD, "gmail_message_id": gid}, content_type="application/json",
                    HTTP_X_INTERNAL_TOKEN=TOKEN, HTTP_X_N8N_EXECUTION_ID="555")
    body = api("post", "/failures", {"workflow": "WF1 Intake", "node": "Label ai-processing", "error": "x",
                                     "execution_id": "555"}).json()
    assert body["email_id"] is None
    assert not Email.objects.filter(status="failed").exists()


def _today(api, fake_llm, gid, draft=True, **llm):
    payload = {**PAYLOAD, "gmail_message_id": gid, "date": timezone.now().isoformat()}
    email_id, _ = _triaged(api, fake_llm, payload, **llm)
    if draft:
        api("post", f"/emails/{email_id}/draft")
    return email_id


def _send(api, email_id, **approval):
    assert api("post", f"/emails/{email_id}/approval", {"decision": "approved", **approval}).status_code == 200
    api("post", f"/emails/{email_id}/actions", {"kind": "reply_sent", "idempotency_key": f"reply_sent:{email_id}"})
    assert api("post", f"/emails/{email_id}/status", {"status": "done"}).json()["status"] == "done"


def _fail(api, email_id):
    body = api("post", "/failures", {"workflow": "WF3", "node": "Gmail send", "error": "timeout",
                                     "email_id": email_id}).json()
    assert body["email_status"] == "failed"


def test_failed_then_retried_reply_counts_as_automatic(api, fake_llm):
    # Failed while sending; Retry resumes at sending with the same Slack approval.
    sent_retry = _today(api, fake_llm, "late")
    api("post", f"/emails/{sent_retry}/approval", {"decision": "approved"})
    _fail(api, sent_retry)
    _send(api, sent_retry)
    # Failed before drafting; Retry started over (failed -> received -> triage -> draft).
    early_retry = _today(api, fake_llm, "early", draft=False)
    _fail(api, early_retry)
    api("post", f"/emails/{early_retry}/status", {"status": "received"})
    api("post", f"/emails/{early_retry}/triage")
    assert api("post", f"/emails/{early_retry}/draft").json()["status"] == "awaiting_approval"
    _send(api, early_retry)
    # Failed right now: counted under failures, not as reviewed.
    still_failed = _today(api, fake_llm, "stuck")
    _fail(api, still_failed)

    assert Email.objects.get(pk=sent_retry).needs_review_reason  # the failure message is still there
    m = metrics.compute("today")
    assert m["handled"] == 3 and m["replies_sent"] == 2 and m["automatic"] == 2
    assert m["reviewed"] == 0
    assert m["failures"] == 3 and m["open_failures"] == 3
    assert "fully automatic 2, reviewed or edited 0" in metrics.digest_text(m)


def test_review_routed_reply_counts_as_reviewed(api, fake_llm):
    # Triage routed it to review; a person approved a draft unchanged from the dashboard.
    routed = _today(api, fake_llm, "routed", draft=False, classification={"category": "other"})
    assert Email.objects.get(pk=routed).status == "needs_review"
    Draft.objects.create(email_id=routed, subject="Re: rate pls", body="Thanks for your request.")
    _send(api, routed, channel="dashboard", reviewer="ops")
    # No decision in Slack in time: WF2 moved it to review (the email records that by itself).
    timed_out = _today(api, fake_llm, "timeout")
    api("post", f"/emails/{timed_out}/status", {"status": "needs_review", "reason": "No decision in Slack"})
    assert Email.objects.get(pk=timed_out).needed_review_at is not None
    _send(api, timed_out, channel="dashboard", reviewer="ops")
    # A plain Slack approval of an untouched draft stays automatic.
    plain = _today(api, fake_llm, "plain")
    _send(api, plain)

    assert not Approval.objects.filter(decision="edited").exists()
    m = metrics.compute("today")
    assert m["replies_sent"] == 3 and m["automatic"] == 1 and m["reviewed"] == 2
    assert metrics.needed_human([routed, timed_out, plain]) == {routed, timed_out}


def test_triage_corrected_by_a_person_counts_as_reviewed(api, fake_llm):
    email_id = _today(api, fake_llm, "corrected")
    t = Triage.objects.get(email_id=email_id)
    Triage.objects.create(email_id=email_id, category=t.category, urgency=t.urgency, confidence=1, route=t.route,
                          model_classify="human:ops")
    _send(api, email_id)
    m = metrics.compute("today")
    assert m["automatic"] == 0 and m["reviewed"] == 1


def test_stale_slack_approval_then_dashboard_approve_counts_as_reviewed(api, fake_llm):
    # The Slack card showed the first draft; someone re-drafted in the dashboard, then the old
    # Slack Approve was refused (stale) and the email went to review without a review alert.
    email_id = _today(api, fake_llm, "stale")
    shown = Email.objects.get(pk=email_id).latest_draft.id
    services.create_draft(Email.objects.get(pk=email_id))  # dashboard Re-draft (ok draft)
    refused = api("post", f"/emails/{email_id}/approval", {"decision": "approved", "draft_id": shown})
    assert refused.status_code == 409 and refused.json()["stale_draft"] is True
    email = Email.objects.get(pk=email_id)
    assert email.status == "needs_review" and email.needed_review_at is not None
    assert not Action.objects.filter(kind="slack_alert").exists() and not Draft.objects.filter(ok=False).exists()
    # Approved unchanged from the dashboard: still counted as reviewed.
    _send(api, email_id, channel="dashboard", reviewer="ops")
    assert Approval.objects.get(email_id=email_id).decision == "approved"
    m = metrics.compute("today")
    assert m["replies_sent"] == 1 and m["automatic"] == 0 and m["reviewed"] == 1


def test_needed_review_at_is_set_once_and_never_cleared(api, fake_llm):
    email_id = _today(api, fake_llm, "once", draft=False, classification={"category": "other"})
    first = Email.objects.get(pk=email_id).needed_review_at
    assert first is not None
    # Re-drafted after a correction: needs_review -> awaiting_approval -> needs_review again.
    state.transition(Email.objects.get(pk=email_id), "awaiting_approval")
    assert Email.objects.get(pk=email_id).needed_review_at == first
    state.transition(Email.objects.get(pk=email_id), "needs_review", reason="again")
    assert Email.objects.get(pk=email_id).needed_review_at == first
    # A failure doesn't set it.
    plain = _today(api, fake_llm, "plain-fail")
    _fail(api, plain)
    assert Email.objects.get(pk=plain).needed_review_at is None
