"""Data migrations: existing rows get the new fields filled in."""

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


@pytest.fixture
def migrator(transactional_db):
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes("inbox")[0]
    yield _migrate
    _migrate(latest)


def _email(Email, gid, **kwargs):
    return Email.objects.create(gmail_message_id=gid, from_email="a@b.example", received_at=timezone.now(), **kwargs)


def test_needed_review_at_backfill(migrator):
    apps = migrator(("inbox", "0003_email_last_execution"))
    Email, Triage, Draft, Action = (apps.get_model("inbox", m) for m in ("Email", "Triage", "Draft", "Action"))
    routed = _email(Email, "routed", status="done")
    Triage.objects.create(email=routed, category="other", urgency="normal", confidence=0.5, route="review")
    blocked = _email(Email, "blocked", status="rejected")
    Draft.objects.create(email=blocked, body="", ok=False)
    alerted = _email(Email, "alerted", status="done")
    Action.objects.create(email=alerted, kind="slack_alert", idempotency_key=f"slack_alert:{alerted.id}:review")
    _email(Email, "stale", status="done",
           needs_review_reason="Slack approval was for an older draft: the draft was re-done ...")
    _email(Email, "waiting", status="needs_review")
    plain = _email(Email, "plain", status="done")
    Triage.objects.create(email=plain, category="quote_request", urgency="normal", confidence=0.9, route="auto")
    Action.objects.create(email=plain, kind="slack_alert", idempotency_key=f"slack_alert:{plain.id}:urgent")
    _email(Email, "failed", status="failed", needs_review_reason="WF2 failed at HubSpot: 401")

    apps = migrator(("inbox", "0004_email_needed_review_at"))
    Email = apps.get_model("inbox", "Email")
    marked = {e.gmail_message_id for e in Email.objects.filter(needed_review_at__isnull=False)}
    assert marked == {"routed", "blocked", "alerted", "stale", "waiting"}
    routed_at = Email.objects.get(gmail_message_id="routed").needed_review_at
    assert abs(routed_at - timezone.now()) < timedelta(minutes=5)
