import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from inbox.models import Action, Triage

from .test_m1_intake import PAYLOAD


def _email(api):
    return api("post", "/emails", PAYLOAD).json()["id"]


def _claim(api, email_id, key, kind="hubspot_deal"):
    return api("post", f"/emails/{email_id}/actions/claim", {"kind": kind, "idempotency_key": key}).json()


def test_claim_lifecycle(api):
    email_id = _email(api)
    key = f"hubspot_deal:{email_id}"
    assert _claim(api, email_id, key) == {"proceed": True, "done": False, "response": {}}
    # a second run while the first is still working: skip
    assert _claim(api, email_id, key) == {"proceed": False, "done": False, "response": {}}
    api("post", f"/emails/{email_id}/actions", {"kind": "hubspot_deal", "idempotency_key": key, "response": {"id": "77"}})
    assert _claim(api, email_id, key) == {"proceed": False, "done": True, "response": {"id": "77"}}
    assert Action.objects.get().ok is True


def test_failed_attempt_can_be_reclaimed(api):
    email_id = _email(api)
    key = f"shipmatch_upload:{email_id}:a1"
    _claim(api, email_id, key, "shipmatch_upload")
    api("post", f"/emails/{email_id}/actions", {"kind": "shipmatch_upload", "idempotency_key": key, "ok": False,
                                                 "response": {"status": 402}})
    assert _claim(api, email_id, key, "shipmatch_upload")["proceed"] is True


def test_stale_claim_can_be_reclaimed(api):
    email_id = _email(api)
    key = f"hubspot_task:{email_id}"
    _claim(api, email_id, key, "hubspot_task")
    old = (timezone.now() - timedelta(minutes=11)).isoformat()
    Action.objects.filter(idempotency_key=key).update(response={"pending": True, "claimed_at": old})
    assert _claim(api, email_id, key, "hubspot_task")["proceed"] is True


def test_claim_rejects_unknown_kind(api):
    email_id = _email(api)
    assert api("post", f"/emails/{email_id}/actions/claim", {"kind": "x", "idempotency_key": "k"}).status_code == 422


@pytest.mark.django_db(transaction=True)
def test_parallel_triage_calls_llm_once(fake_llm, settings):
    """Two WF2 runs for the same email at the same moment: one LLM triage, one row."""
    import json

    from django.test import Client

    from .conftest import TOKEN

    llm = fake_llm()
    original = llm.structured
    gate = threading.Event()

    def slow_structured(**kwargs):
        gate.wait(0.3)  # keep the first call inside the lock while the second arrives
        return original(**kwargs)

    llm.structured = slow_structured
    client = Client()
    headers = {"HTTP_X_INTERNAL_TOKEN": TOKEN}
    email_id = client.post("/internal/emails", data=json.dumps(PAYLOAD), content_type="application/json",
                           **headers).json()["id"]
    results = []

    def run():
        try:
            response = Client().post(f"/internal/emails/{email_id}/triage", **headers)
            results.append((response.status_code, response.json().get("rerun")))
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [(200, False), (200, True)]
    assert Triage.objects.filter(email_id=email_id).count() == 1
    assert len(llm.calls) == 2  # classify + extract, once


def test_failure_releases_pending_claims(api):
    email_id = _email(api)
    key = f"hubspot_deal:{email_id}"
    _claim(api, email_id, key)
    assert _claim(api, email_id, key)["proceed"] is False  # held by the crashed run
    api("post", "/failures", {"workflow": "WF2", "node": "HubSpot create deal", "error": "401", "email_id": email_id})
    api("post", f"/emails/{email_id}/status", {"status": "received"})  # Retry
    assert _claim(api, email_id, key)["proceed"] is True


def test_retry_releases_claims_but_keeps_done_actions(api):
    email_id = _email(api)
    done_key, open_key = f"hubspot_contact:{email_id}", f"hubspot_deal:{email_id}"
    api("post", f"/emails/{email_id}/actions", {"kind": "hubspot_contact", "idempotency_key": done_key, "response": {"id": "1"}})
    _claim(api, email_id, open_key)
    api("post", f"/emails/{email_id}/status", {"status": "failed", "reason": "x"})
    api("post", f"/emails/{email_id}/status", {"status": "received"})
    assert _claim(api, email_id, open_key)["proceed"] is True
    assert _claim(api, email_id, done_key, "hubspot_contact")["done"] is True
