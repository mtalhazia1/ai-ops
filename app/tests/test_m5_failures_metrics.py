from datetime import timedelta

from django.utils import timezone

from inbox import metrics
from inbox.models import Action, Email, Failure, Triage

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
