import pytest

from inbox import services
from inbox.models import Email, Failure, Triage

from .test_m4_draft import THREADED, _awaiting, _triaged


@pytest.fixture
def n8n_calls(monkeypatch, settings):
    settings.N8N_EXECUTE_WEBHOOK_URL = "http://n8n/webhook/execute"
    settings.N8N_PROCESS_WEBHOOK_URL = "http://n8n/webhook/process"
    calls = []
    monkeypatch.setattr(services, "call_n8n", lambda url, payload: calls.append((url, payload)))
    return calls


def test_review_queue_lists_only_waiting_emails(admin_client, api, fake_llm):
    review_id, _ = _triaged(api, fake_llm, classification={"category": "other"})
    page = admin_client.get("/").content.decode()
    assert f"/emails/{review_id}/" in page
    rows = admin_client.get("/?status=needs_review", HTTP_HX_REQUEST="true").content.decode()
    assert "<html" not in rows and f"/emails/{review_id}/" in rows


def test_approve_from_dashboard_calls_wf3(admin_client, api, fake_llm, n8n_calls):
    email_id = _awaiting(api, fake_llm)
    response = admin_client.post(f"/emails/{email_id}/decide/", {"action": "approve", "final_body": "Hi Sarah,\nThanks.\nOps"})
    assert response.status_code == 302
    url, payload = n8n_calls[0]
    assert url.endswith("/webhook/execute")
    assert payload == {"email_id": email_id, "decision": "approved", "reviewer": "admin", "channel": "dashboard",
                       "final_body": "Hi Sarah,\nThanks.\nOps"}


def test_dashboard_approve_blocks_failing_reply_until_confirmed(admin_client, api, fake_llm, n8n_calls):
    email_id = _awaiting(api, fake_llm)
    body = "Rate is $450, pay at https://pay.example"
    admin_client.post(f"/emails/{email_id}/decide/", {"action": "approve", "final_body": body})
    assert n8n_calls == []
    admin_client.post(f"/emails/{email_id}/decide/", {"action": "approve", "final_body": body, "confirm": "on"})
    assert n8n_calls[0][1]["final_body"] == body


def test_reject_and_ignore(admin_client, api, fake_llm, n8n_calls):
    email_id = _awaiting(api, fake_llm)
    admin_client.post(f"/emails/{email_id}/decide/", {"action": "reject"})
    assert n8n_calls[0][1]["decision"] == "rejected"
    other_id, _ = _triaged(api, fake_llm, {**THREADED, "gmail_message_id": "m-other"}, classification={"category": "other"})
    admin_client.post(f"/emails/{other_id}/decide/", {"action": "ignore"})
    assert Email.objects.get(pk=other_id).status == "ignored"


def test_cannot_decide_on_done_email(admin_client, api, fake_llm, n8n_calls):
    email_id, _ = _triaged(api, fake_llm)  # triaged, not waiting for a person
    admin_client.post(f"/emails/{email_id}/decide/", {"action": "approve", "final_body": "x"})
    assert n8n_calls == []


def test_check_draft_htmx(admin_client, api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    bad = admin_client.post(f"/emails/{email_id}/check/", {"final_body": "Write to x@evil.example"}).content.decode()
    assert "new email address" in bad
    good = admin_client.post(f"/emails/{email_id}/check/", {"final_body": "Thanks, noted."}).content.decode()
    assert "Passes the checks" in good


def test_edit_triage_creates_new_row_and_recomputes_missing(admin_client, api, fake_llm):
    email_id, _ = _triaged(api, fake_llm, classification={"category": "other"})
    # other has no fields; switch to quote_request first
    admin_client.post(f"/emails/{email_id}/triage/", {"category": "quote_request", "urgency": "normal"})
    t = Email.objects.get(pk=email_id).latest_triage
    assert t.category == "quote_request" and t.model_classify == "human:admin"
    assert "origin_city" in t.missing_fields
    admin_client.post(f"/emails/{email_id}/triage/", {
        "category": "quote_request", "urgency": "normal", "f_origin_city": "Lahore", "f_destination_city": "Karachi",
        "f_pickup_date": "2026-10-13", "f_pieces": "4", "f_hazardous": "false", "f_special_requirements": "tail lift, insured"})
    t = Email.objects.get(pk=email_id).latest_triage
    assert t.fields["pickup_date"] == "2026-10-13" and t.fields["pieces"] == 4 and t.fields["hazardous"] is False
    assert t.fields["special_requirements"] == ["tail lift", "insured"]
    assert t.missing_fields == []
    assert Triage.objects.filter(email_id=email_id).count() == 3
    assert Email.objects.get(pk=email_id).category == "quote_request"


def test_redraft_from_dashboard(admin_client, api, fake_llm):
    email_id, llm = _triaged(api, fake_llm, classification={"category": "other"})
    admin_client.post(f"/emails/{email_id}/redraft/")
    assert Email.objects.get(pk=email_id).drafts.count() == 0  # "other" has no template
    admin_client.post(f"/emails/{email_id}/triage/", {"category": "booking", "urgency": "normal"})
    admin_client.post(f"/emails/{email_id}/redraft/")
    email = Email.objects.get(pk=email_id)
    assert email.drafts.count() == 1 and email.status == "needs_review"  # still with the reviewer


def test_detail_page_shows_workspace(admin_client, api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    page = admin_client.get(f"/emails/{email_id}/").content.decode()
    assert "Approve and send" in page and "Timeline" in page and "Draft passed checks" in page
    assert 'name="f_origin_city"' in page


def test_retry_without_decision_reruns_wf2_and_reuses_triage(admin_client, api, fake_llm, n8n_calls):
    email_id, llm = _triaged(api, fake_llm)
    api("post", "/failures", {"workflow": "WF2 Process", "node": "HubSpot create deal", "error": "401", "email_id": email_id})
    admin_client.post(f"/emails/{email_id}/retry/")
    assert n8n_calls == [("http://n8n/webhook/process", {"email_id": email_id})]
    assert Email.objects.get(pk=email_id).status == "received"
    assert Failure.objects.get().resolved is True
    calls = len(llm.calls)
    body = api("post", f"/emails/{email_id}/triage").json()
    assert body["rerun"] is True and body["status"] == "triaged" and len(llm.calls) == calls


def test_retry_after_approval_resumes_sending(admin_client, api, fake_llm, n8n_calls):
    email_id = _awaiting(api, fake_llm)
    api("post", f"/emails/{email_id}/approval", {"decision": "approved", "reviewer": "slack"})
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Gmail send reply", "error": "503", "email_id": email_id})
    admin_client.post(f"/emails/{email_id}/retry/")
    url, payload = n8n_calls[0]
    assert url.endswith("/webhook/execute") and payload["decision"] == "approved"
    assert Email.objects.get(pk=email_id).status == "failed"  # WF3 moves it to approved


def test_retry_only_for_failed(admin_client, api, fake_llm, n8n_calls):
    email_id, _ = _triaged(api, fake_llm)
    admin_client.post(f"/emails/{email_id}/retry/")
    assert n8n_calls == []


def test_failures_page_and_resolve(admin_client, api, fake_llm):
    email_id, _ = _triaged(api, fake_llm)
    api("post", "/failures", {"workflow": "WF2 Process", "node": "HubSpot", "error": "401 Unauthorized", "email_id": email_id})
    page = admin_client.get("/failures/").content.decode()
    assert "401 Unauthorized" in page and "Retry" in page
    admin_client.post(f"/failures/{Failure.objects.get().pk}/resolve/")
    assert "401 Unauthorized" not in admin_client.get("/failures/").content.decode()
    assert "401 Unauthorized" in admin_client.get("/failures/?all=1").content.decode()


def test_pages_need_login(client, db):
    for url in ["/", "/emails/", "/failures/", "/metrics/", "/playbook/", "/evals/"]:
        assert client.get(url).status_code == 302, url
