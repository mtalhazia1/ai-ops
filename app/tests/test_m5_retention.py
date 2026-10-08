from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from inbox.models import Draft, Email

from .test_m1_intake import PAYLOAD
from .test_m4_draft import _awaiting


def test_purge_old_bodies_keeps_rows(api, fake_llm):
    old_id = _awaiting(api, fake_llm)
    Email.objects.filter(pk=old_id).update(received_at=timezone.now() - timedelta(days=91))
    new_id = api("post", "/emails", {**PAYLOAD, "gmail_message_id": "new"}).json()["id"]
    call_command("purge_old_bodies", "--dry-run")
    assert "pallets" in Email.objects.get(pk=old_id).body_text
    call_command("purge_old_bodies")
    old = Email.objects.get(pk=old_id)
    assert old.body_text.startswith("[deleted") and old.category == "quote_request" and old.status == "awaiting_approval"
    assert Draft.objects.get(email_id=old_id).body.startswith("[deleted")
    assert old.triages.count() == 1  # costs and metrics stay
    assert "pallets" in Email.objects.get(pk=new_id).body_text
