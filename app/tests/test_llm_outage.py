"""An Anthropic outage (connection error, timeout, 429/5xx after the SDK's retries) fails the
run so Retry repeats it, like a transient ShipMatch error; it never becomes a review item."""

from types import SimpleNamespace

import anthropic
import pytest

from inbox.llm import client as llm_client
from inbox.llm.client import AnthropicLLM, LLMError, LLMUnavailable
from inbox.models import Draft, Email, Failure, Triage

from .conftest import FakeLLM
from .test_m1_intake import PAYLOAD
from .test_m4_draft import _awaiting, _triaged
from .test_m2_llm_client import _call

# Stand-ins for the SDK's HTTP request/response objects (only these attributes are read).
REQUEST = SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages")


def _status_error(cls, status):
    response = SimpleNamespace(status_code=status, request=REQUEST, headers={})
    return cls("mock outage", response=response, body=None)


def _raising_client(exc):
    def create(**kwargs):
        raise exc
    return SimpleNamespace(messages=SimpleNamespace(create=create))


@pytest.mark.parametrize("exc", [
    _status_error(anthropic.InternalServerError, 500),
    _status_error(anthropic.InternalServerError, 529),
    _status_error(anthropic.RateLimitError, 429),
    anthropic.APIConnectionError(request=REQUEST),
    anthropic.APITimeoutError(request=REQUEST),
])
def test_transient_api_errors_are_unavailable(exc):
    with pytest.raises(LLMUnavailable) as raised:
        _call(AnthropicLLM(_raising_client(exc)))
    assert type(exc).__name__ in str(raised.value)


@pytest.mark.parametrize("exc", [
    _status_error(anthropic.BadRequestError, 400),
    _status_error(anthropic.AuthenticationError, 401),
    ValueError("not an API error"),
])
def test_other_errors_are_not_unavailable(exc):
    with pytest.raises(LLMError) as raised:
        _call(AnthropicLLM(_raising_client(exc)))
    assert not isinstance(raised.value, LLMUnavailable)


class OutageLLM(FakeLLM):
    """FakeLLM whose calls of the given kinds ("classify", "extract", "draft") raise."""

    def __init__(self, down=("draft",), error=LLMUnavailable, **kwargs):
        super().__init__(**kwargs)
        self.down, self.error = set(down), error

    def structured(self, *, schema, **kwargs):
        props = schema["properties"]
        kind = "draft" if "body" in props else "classify" if "category" in props else "extract"
        if kind in self.down:
            self.calls.append({"kind": kind, "failed": True})
            raise self.error("InternalServerError: mock outage")
        return super().structured(schema=schema, **kwargs)


def _outage(**kwargs):
    llm = OutageLLM(**kwargs)
    llm_client.set_llm(llm)
    return llm


def test_draft_outage_answers_503_and_saves_nothing(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm)
    _outage(down=("draft",))
    response = api("post", f"/emails/{email_id}/draft")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False and body["retry"] is True and "AI service unavailable" in body["detail"]
    assert "InternalServerError" in body["detail"]
    email = Email.objects.get(pk=email_id)
    assert email.status == "triaged" and not email.needs_review_reason
    assert not Draft.objects.filter(email_id=email_id).exists()


def test_draft_outage_then_failure_and_retry_drafts(api, fake_llm, settings, monkeypatch):
    from inbox import services
    settings.N8N_PROCESS_WEBHOOK_URL = "http://n8n/webhook/process"
    monkeypatch.setattr(services, "call_n8n", lambda url, payload: None)
    email_id, _ = _triaged(api, fake_llm)
    _outage(down=("draft",))
    assert api("post", f"/emails/{email_id}/draft").status_code == 503
    # WF2 stops at "Draft failed"; WF4 records it.
    failure = api("post", "/failures", {"workflow": "WF2 Process", "node": "Draft failed",
                                        "error": "Draft not ready: AI service unavailable", "email_id": email_id}).json()
    assert failure["email_status"] == "failed"
    # Retry after the outage: the saved triage is reused, only the draft costs an LLM call.
    llm = fake_llm()
    assert services.retry(Email.objects.get(pk=email_id)) == "process"
    body = api("post", f"/emails/{email_id}/triage").json()
    assert body["rerun"] is True and body["status"] == "triaged"
    draft = api("post", f"/emails/{email_id}/draft").json()
    assert draft["ok"] is True and draft["status"] == "awaiting_approval"
    assert len(llm.calls) == 1 and Triage.objects.filter(email_id=email_id).count() == 1
    assert Failure.objects.get(email_id=email_id).resolved is True


def test_non_transient_draft_error_still_goes_to_review(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm)
    _outage(down=("draft",), error=LLMError)
    body = api("post", f"/emails/{email_id}/draft").json()
    assert body["ok"] is False and body["status"] == "needs_review"
    assert "drafting failed" in Email.objects.get(pk=email_id).needs_review_reason


@pytest.mark.parametrize("down", [("classify",), ("extract",)])
def test_triage_outage_answers_503_and_saves_nothing(api, down):
    email_id = api("post", "/emails", PAYLOAD).json()["id"]
    _outage(down=down)
    response = api("post", f"/emails/{email_id}/triage")
    assert response.status_code == 503
    body = response.json()
    assert body["retry"] is True and "AI service unavailable" in body["detail"]
    assert "current" not in body and "category" not in body  # WF2: not a triage result, not "already past"
    assert Email.objects.get(pk=email_id).status == "received"
    assert not Triage.objects.filter(email_id=email_id).exists()
    # WF4 moves it to failed; after Retry the triage runs again.
    assert api("post", "/failures", {"workflow": "WF2", "node": "Triage failed", "error": "x",
                                     "email_id": email_id}).json()["email_status"] == "failed"
    api("post", f"/emails/{email_id}/status", {"status": "received"})
    llm_client.set_llm(FakeLLM())
    assert api("post", f"/emails/{email_id}/triage").json()["status"] == "triaged"


def test_non_transient_classify_error_still_goes_to_review(api):
    email_id = api("post", "/emails", PAYLOAD).json()["id"]
    _outage(down=("classify",), error=LLMError)
    body = api("post", f"/emails/{email_id}/triage").json()
    assert body["route"] == "review" and body["status"] == "needs_review"


def test_dashboard_redraft_during_outage_keeps_the_draft(admin_client, api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    _outage(down=("draft",))
    page = admin_client.post(f"/emails/{email_id}/redraft/", follow=True).content.decode()
    assert "Not re-drafted: the AI service is unavailable right now" in page
    email = Email.objects.get(pk=email_id)
    assert email.drafts.count() == 1 and email.latest_draft.ok and email.status == "awaiting_approval"
