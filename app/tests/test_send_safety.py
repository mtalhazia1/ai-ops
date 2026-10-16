"""Retry and send safety (code review 2026-10-16, #13 and #18)."""

import pytest

from inbox import services
from inbox.models import Email, Failure

from .test_m4_draft import _awaiting, _triaged


@pytest.fixture
def n8n_down(monkeypatch, settings):
    settings.N8N_EXECUTE_WEBHOOK_URL = "http://n8n/webhook/execute"
    settings.N8N_PROCESS_WEBHOOK_URL = "http://n8n/webhook/process"

    def fail(url, payload):
        raise services.N8nError("n8n unreachable: connection refused")

    monkeypatch.setattr(services, "call_n8n", fail)


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
