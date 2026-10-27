from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from inbox.mail import PURGED
from inbox.models import Approval, Draft, Email

from .test_m1_intake import PAYLOAD
from .test_m4_draft import THREADED, _awaiting, _decode


def _age(email_id, days=91):
    Email.objects.filter(pk=email_id).update(received_at=timezone.now() - timedelta(days=days))


def _drafted(api, gmail_message_id):
    email_id = api("post", "/emails", {**THREADED, "gmail_message_id": gmail_message_id}).json()["id"]
    api("post", f"/emails/{email_id}/triage")
    api("post", f"/emails/{email_id}/draft")
    return email_id


def test_purge_old_bodies_keeps_rows(api, fake_llm):
    old_id = _awaiting(api, fake_llm)
    api("post", f"/emails/{old_id}/approval", {"decision": "approved"})
    api("post", f"/emails/{old_id}/actions", {"kind": "reply_sent", "idempotency_key": f"reply_sent:{old_id}"})
    api("post", f"/emails/{old_id}/status", {"status": "done"})
    _age(old_id)
    new_id = api("post", "/emails", {**PAYLOAD, "gmail_message_id": "new"}).json()["id"]
    call_command("purge_old_bodies", "--dry-run")
    assert "pallets" in Email.objects.get(pk=old_id).body_text
    call_command("purge_old_bodies")
    old = Email.objects.get(pk=old_id)
    assert old.body_text == PURGED and old.category == "quote_request" and old.status == "done"
    assert Draft.objects.get(email_id=old_id).body == PURGED
    assert Approval.objects.get(email_id=old_id).final_body == PURGED
    assert old.triages.count() == 1  # costs and metrics stay
    assert "pallets" in Email.objects.get(pk=new_id).body_text


def test_purge_skips_open_emails(api, fake_llm):
    """An old email still awaiting approval, or failed after its approval, keeps its text:
    a later approval or Retry must send the real reply."""
    awaiting_id = _awaiting(api, fake_llm)
    failed_id = _drafted(api, "m-failed")
    api("post", f"/emails/{failed_id}/approval", {"decision": "approved"})
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Record decision", "error": "500", "email_id": failed_id})
    rejected_id = _drafted(api, "m-rejected")
    api("post", f"/emails/{rejected_id}/approval", {"decision": "rejected"})
    for email_id in (awaiting_id, failed_id, rejected_id):
        _age(email_id)
    call_command("purge_old_bodies")
    for email_id in (awaiting_id, failed_id):
        assert "pallets" in Email.objects.get(pk=email_id).body_text
        assert Draft.objects.get(email_id=email_id).body != PURGED
    assert Approval.objects.get(email_id=failed_id).final_body != PURGED
    assert Email.objects.get(pk=rejected_id).body_text == PURGED  # final: purged
    # Retry of the failed one resumes at sending with the real text.
    raw = api("post", f"/emails/{failed_id}/approval", {"decision": "approved"}).json()["raw"]
    assert "[deleted" not in _decode(raw).get_content()


def test_purged_reply_is_never_sent(api, fake_llm):
    """Defence in depth: a reply text that was already blanked (e.g. by the older purge)
    is refused with 409, and nothing is recorded."""
    email_id = _awaiting(api, fake_llm)
    Draft.objects.filter(email_id=email_id).update(body=PURGED)
    response = api("post", f"/emails/{email_id}/approval", {"decision": "approved"})
    assert response.status_code == 409 and "retention" in response.json()["detail"]
    assert Email.objects.get(pk=email_id).status == "awaiting_approval"
    assert not Approval.objects.filter(email_id=email_id).exists()
    # A failed send whose saved approval was blanked can't resume either.
    failed_id = _drafted(api, "m-blanked")
    api("post", f"/emails/{failed_id}/approval", {"decision": "approved"})
    api("post", f"/emails/{failed_id}/status", {"status": "failed", "reason": "x"})
    Approval.objects.filter(email_id=failed_id).update(final_body=PURGED)
    assert api("post", f"/emails/{failed_id}/approval", {"decision": "approved"}).status_code == 409
    assert Email.objects.get(pk=failed_id).status == "failed"
