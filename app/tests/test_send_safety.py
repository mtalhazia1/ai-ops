"""Retry and send safety (code review 2026-10-16, #13 and #18)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from inbox import services
from inbox.models import Action, Email, Failure

from .test_m4_draft import _awaiting, _triaged


@pytest.fixture
def n8n_calls(monkeypatch, settings):
    settings.N8N_EXECUTE_WEBHOOK_URL = "http://n8n/webhook/execute"
    settings.N8N_PROCESS_WEBHOOK_URL = "http://n8n/webhook/process"
    calls = []
    monkeypatch.setattr(services, "call_n8n", lambda url, payload: calls.append((url, payload)))
    return calls


@pytest.fixture
def n8n_down(monkeypatch, settings):
    settings.N8N_EXECUTE_WEBHOOK_URL = "http://n8n/webhook/execute"
    settings.N8N_PROCESS_WEBHOOK_URL = "http://n8n/webhook/process"

    def fail(url, payload):
        raise services.N8nError("n8n unreachable: connection refused")

    monkeypatch.setattr(services, "call_n8n", fail)


def _claim_reply(api, email_id):
    return api("post", f"/emails/{email_id}/actions/claim",
               {"kind": "reply_sent", "idempotency_key": f"reply_sent:{email_id}"}).json()


def _approved_and_claimed(api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    api("post", f"/emails/{email_id}/approval", {"decision": "approved", "reviewer": "slack"})
    assert _claim_reply(api, email_id)["proceed"] is True
    return email_id


# --- #13: Retry when n8n can't be reached ---------------------------------------------

def test_process_retry_with_n8n_down_stays_failed(admin_client, api, fake_llm, n8n_down):
    email_id, _ = _triaged(api, fake_llm)
    api("post", "/failures", {"workflow": "WF2 Process", "node": "HubSpot", "error": "401", "email_id": email_id})
    reason = Email.objects.get(pk=email_id).needs_review_reason
    response = admin_client.post(f"/emails/{email_id}/retry/", follow=True)
    assert "Retry failed" in response.content.decode()
    email = Email.objects.get(pk=email_id)
    assert email.status == "failed" and email.needs_review_reason == reason
    assert Failure.objects.get().resolved is False


def test_process_retry_with_n8n_down_can_be_retried_again(admin_client, api, fake_llm, n8n_down, monkeypatch):
    email_id, _ = _triaged(api, fake_llm)
    api("post", "/failures", {"workflow": "WF2 Process", "node": "HubSpot", "error": "401", "email_id": email_id})
    with pytest.raises(services.N8nError):
        services.retry(Email.objects.get(pk=email_id))
    calls = []
    monkeypatch.setattr(services, "call_n8n", lambda url, payload: calls.append(url))
    assert services.retry(Email.objects.get(pk=email_id)) == "process"
    assert calls == ["http://n8n/webhook/process"] and Email.objects.get(pk=email_id).status == "received"


def test_process_retry_failure_does_not_undo_a_run_that_started(api, fake_llm, monkeypatch, settings):
    """n8n took the call (WF2 already triaged) but the answer was lost: leave the run alone."""
    settings.N8N_PROCESS_WEBHOOK_URL = "http://n8n/webhook/process"
    email_id, _ = _triaged(api, fake_llm)
    api("post", "/failures", {"workflow": "WF2 Process", "node": "HubSpot", "error": "401", "email_id": email_id})

    def started_then_timed_out(url, payload):
        api("post", f"/emails/{email_id}/triage")
        raise services.N8nError("n8n unreachable: timed out")

    monkeypatch.setattr(services, "call_n8n", started_then_timed_out)
    with pytest.raises(services.N8nError):
        services.retry(Email.objects.get(pk=email_id))
    assert Email.objects.get(pk=email_id).status == "triaged"


def test_send_retry_with_n8n_down_stays_failed(admin_client, api, fake_llm, n8n_down):
    email_id = _awaiting(api, fake_llm)
    api("post", f"/emails/{email_id}/approval", {"decision": "approved", "reviewer": "slack"})
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Record decision", "error": "500", "email_id": email_id})
    admin_client.post(f"/emails/{email_id}/retry/")
    assert Email.objects.get(pk=email_id).status == "failed"
    assert Failure.objects.get().resolved is False


# --- #18: a send whose result was never logged ----------------------------------------

def test_failure_after_reply_claim_is_not_resent(api, fake_llm):
    email_id = _approved_and_claimed(api, fake_llm)
    # Gmail sent the reply, then "Log reply" failed; WF4 records the failure.
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Log reply", "error": "500", "email_id": email_id})
    email = Email.objects.get(pk=email_id)
    assert email.status == "failed" and email.needs_review_reason.startswith("reply may already have been sent")
    # Slack re-click / WF3 re-run: the decision is replayed, but the send is not.
    assert api("post", f"/emails/{email_id}/approval", {"decision": "approved"}).json()["rerun"] is True
    claim = _claim_reply(api, email_id)
    assert claim["proceed"] is False and claim["done"] is False and claim["uncertain"] is True
    assert Email.objects.get(pk=email_id).status == "failed"
    # Still not after the 10-minute claim TTL.
    Action.objects.filter(kind="reply_sent").update(
        response={"uncertain": True, "claimed_at": (timezone.now() - timedelta(hours=1)).isoformat()})
    assert _claim_reply(api, email_id)["proceed"] is False


def test_expired_reply_claim_is_not_reclaimable(api, fake_llm):
    email_id = _approved_and_claimed(api, fake_llm)
    # The worker died after the send; no failure recorded. The claim expires.
    old = (timezone.now() - timedelta(minutes=11)).isoformat()
    Action.objects.filter(kind="reply_sent").update(response={"pending": True, "claimed_at": old})
    claim = _claim_reply(api, email_id)
    assert claim["proceed"] is False and claim["uncertain"] is True
    email = Email.objects.get(pk=email_id)
    assert email.status == "failed" and "check the Gmail thread" in email.needs_review_reason
    assert _claim_reply(api, email_id)["proceed"] is False


def test_fresh_reply_claim_still_means_in_progress(api, fake_llm):
    email_id = _approved_and_claimed(api, fake_llm)
    assert _claim_reply(api, email_id) == {"proceed": False, "done": False, "response": {}}
    assert Email.objects.get(pk=email_id).status == "approved"


def test_retry_refuses_when_reply_may_have_been_sent(admin_client, api, fake_llm, n8n_calls):
    email_id = _approved_and_claimed(api, fake_llm)
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Gmail send reply", "error": "timeout", "email_id": email_id})
    with pytest.raises(services.ReplyMaybeSent):
        services.retry(Email.objects.get(pk=email_id))
    page = admin_client.post(f"/emails/{email_id}/retry/", follow=True).content.decode()
    assert "Not retried" in page and "Mark as sent" in page and "Send again anyway" in page
    assert n8n_calls == []
    assert Failure.objects.get().resolved is False


def test_mark_as_sent_closes_without_sending(admin_client, api, fake_llm, n8n_calls):
    email_id = _approved_and_claimed(api, fake_llm)
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Log reply", "error": "500", "email_id": email_id})
    admin_client.post(f"/emails/{email_id}/reply/", {"action": "sent"})
    assert Email.objects.get(pk=email_id).status == "done"
    assert n8n_calls == []
    action = Action.objects.get(kind="reply_sent")
    assert action.ok is True and action.response["marked_sent_by"] == "admin"
    assert Failure.objects.get().resolved is True
    assert _claim_reply(api, email_id)["done"] is True


def test_send_again_anyway_sends_once_more(admin_client, api, fake_llm, n8n_calls):
    email_id = _approved_and_claimed(api, fake_llm)
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Gmail send reply", "error": "503", "email_id": email_id})
    admin_client.post(f"/emails/{email_id}/reply/", {"action": "resend"})
    assert n8n_calls and n8n_calls[0][0].endswith("/webhook/execute")
    # WF3: replays the decision, claims, sends.
    api("post", f"/emails/{email_id}/approval", {"decision": "approved"})
    assert _claim_reply(api, email_id)["proceed"] is True
    assert Action.objects.get(kind="reply_sent").response["pending"] is True


def test_reply_buttons_only_for_unconfirmed_sends(admin_client, api, fake_llm, n8n_calls):
    email_id, _ = _triaged(api, fake_llm)
    api("post", "/failures", {"workflow": "WF2 Process", "node": "HubSpot", "error": "401", "email_id": email_id})
    page = admin_client.get(f"/emails/{email_id}/").content.decode()
    assert "Retry" in page and "Mark as sent" not in page
    admin_client.post(f"/emails/{email_id}/reply/", {"action": "resend"})
    assert n8n_calls == [] and Email.objects.get(pk=email_id).status == "failed"


def test_other_claims_are_still_released(api, fake_llm):
    email_id = _approved_and_claimed(api, fake_llm)
    key = f"hubspot_task:{email_id}"
    api("post", f"/emails/{email_id}/actions/claim", {"kind": "hubspot_task", "idempotency_key": key})
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Log reply", "error": "500", "email_id": email_id})
    assert Action.objects.get(idempotency_key=key).response["released"] is True
    assert Action.objects.get(kind="reply_sent").response["uncertain"] is True
