"""An email whose From can't be parsed is kept, but nothing is ever sent or upserted to it."""

import pytest

from inbox import mail, services
from inbox.llm.triage import NO_SENDER_REASON
from inbox.models import Approval, Email

from .test_m1_intake import PAYLOAD

BAD_FROM = {**PAYLOAD, "gmail_message_id": "msg-bad-from", "from": "Sarah Khan via broken gateway"}


@pytest.fixture
def n8n_calls(monkeypatch, settings):
    settings.N8N_EXECUTE_WEBHOOK_URL = "http://n8n/webhook/execute"
    calls = []
    monkeypatch.setattr(services, "call_n8n", lambda url, payload: calls.append((url, payload)))
    return calls


def _bad_sender_in_review(api, fake_llm):
    fake_llm()
    email_id = api("post", "/emails", BAD_FROM).json()["id"]
    body = api("post", f"/emails/{email_id}/triage").json()
    return email_id, body


def test_no_sender_routes_to_review_not_crm(api, fake_llm):
    email_id, body = _bad_sender_in_review(api, fake_llm)
    # WF2 only reaches HubSpot on the "auto" route.
    assert body["route"] == "review" and body["status"] == "needs_review"
    assert NO_SENDER_REASON in body["review_reason"]
    assert NO_SENDER_REASON in Email.objects.get(pk=email_id).needs_review_reason


def test_no_sender_cannot_be_approved(api, fake_llm):
    email_id, _ = _bad_sender_in_review(api, fake_llm)
    response = api("post", f"/emails/{email_id}/approval",
                   {"decision": "approved", "channel": "dashboard", "final_body": "Hi, thanks."})
    assert response.status_code == 422
    assert Email.objects.get(pk=email_id).status == "needs_review"
    assert Approval.objects.count() == 0
    # rejecting is still allowed
    assert api("post", f"/emails/{email_id}/approval", {"decision": "rejected"}).status_code == 200


def test_dashboard_refuses_to_approve_without_sender(admin_client, api, fake_llm, n8n_calls):
    email_id, _ = _bad_sender_in_review(api, fake_llm)
    response = admin_client.post(f"/emails/{email_id}/decide/", {"action": "approve", "final_body": "Hi, thanks."})
    assert response.status_code == 302
    assert n8n_calls == []
    page = admin_client.get(f"/emails/{email_id}/").content.decode()
    assert "No valid sender address" in page


def test_build_reply_refuses_empty_recipient(api):
    email_id = api("post", "/emails", BAD_FROM).json()["id"]
    email = Email.objects.get(pk=email_id)
    assert mail.reply_recipient(email) == ""
    with pytest.raises(mail.NoRecipient):
        mail.build_reply(email, "Re: x", "body")


def test_valid_reply_to_is_used_when_from_is_bad(api):
    email_id = api("post", "/emails", {**BAD_FROM, "reply_to": "desk@acme-textiles.example"}).json()["id"]
    msg = mail.build_reply(Email.objects.get(pk=email_id), "Re: x", "body")
    assert msg["To"] == "desk@acme-textiles.example"
