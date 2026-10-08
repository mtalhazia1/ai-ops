from inbox.models import Action, Email, Failure, Triage

from .test_m1_intake import PAYLOAD


def _create(api):
    return api("post", "/emails", PAYLOAD).json()["id"]


def test_triage_endpoint_saves_row_and_moves_state(api, fake_llm):
    fake_llm(fields={"origin_city": "Lahore"}, evidence={"origin_city": "Lahore"})
    email_id = _create(api)
    response = api("post", f"/emails/{email_id}/triage")
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "auto" and body["status"] == "triaged"
    assert body["category"] == "quote_request"
    assert body["missing_fields"] == ["destination_city", "pickup_date", "weight_kg"]
    triage = Triage.objects.get(email_id=email_id)
    assert triage.input_tokens == 200 and triage.model_classify
    email = Email.objects.get(pk=email_id)
    assert email.category == "quote_request" and email.status == "triaged"


def test_triage_twice_is_409(api, fake_llm):
    fake_llm()
    email_id = _create(api)
    assert api("post", f"/emails/{email_id}/triage").status_code == 200
    response = api("post", f"/emails/{email_id}/triage")
    assert response.status_code == 409
    assert response.json()["current"] == "triaged"
    assert Triage.objects.count() == 1


def test_review_route_sets_needs_review(api, fake_llm):
    fake_llm(classification={"category": "other"})
    email_id = _create(api)
    body = api("post", f"/emails/{email_id}/triage").json()
    assert body["route"] == "review" and body["status"] == "needs_review"
    assert Email.objects.get(pk=email_id).needs_review_reason == "category is other"


def test_invalid_status_change_is_409(api):
    email_id = _create(api)
    assert api("post", f"/emails/{email_id}/status", {"status": "done"}).status_code == 409
    assert api("post", f"/emails/{email_id}/status", {"status": "needs_review", "reason": "manual"}).status_code == 200
    assert api("post", f"/emails/{email_id}/status", {"status": "needs_review"}).status_code == 409


def test_actions_are_idempotent(api):
    email_id = _create(api)
    action = {"kind": "hubspot_deal", "idempotency_key": f"hubspot_deal:{email_id}", "request": {"token": "secret", "name": "x"},
              "response": {"id": "123"}}
    first = api("post", f"/emails/{email_id}/actions", action).json()
    second = api("post", f"/emails/{email_id}/actions", {**action, "response": {"id": "999"}}).json()
    assert first["created"] is True and second["created"] is False
    assert second["response"] == {"id": "123"}  # the original record wins
    assert Action.objects.get().request == {"token": "[redacted]", "name": "x"}
    lookup = api("get", f"/emails/{email_id}/actions?key=hubspot_deal:{email_id}").json()
    assert lookup["exists"] is True
    assert api("get", f"/emails/{email_id}/actions?key=reply_sent:{email_id}").json()["exists"] is False


def test_unknown_action_kind_rejected(api):
    email_id = _create(api)
    assert api("post", f"/emails/{email_id}/actions", {"kind": "rm_rf", "idempotency_key": "k"}).status_code == 422


def test_failure_marks_email_failed_and_retry_resets(api):
    email_id = _create(api)
    body = api("post", "/failures", {"workflow": "WF2", "node": "HubSpot", "error": "401", "execution_id": "77",
                                      "email_id": email_id}).json()
    assert body["email_status"] == "failed"
    assert Failure.objects.get().node == "HubSpot"
    assert api("post", f"/emails/{email_id}/status", {"status": "received"}).json()["status"] == "received"
