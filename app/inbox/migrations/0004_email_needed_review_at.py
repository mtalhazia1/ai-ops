from django.db import migrations, models
from django.db.models import Min

# Reasons written by the paths into needs_review that leave no other trace (see
# api.STALE_DRAFT_REASON and WF2's approval timeout). Matched as substrings.
REVIEW_REASONS = ("Slack approval was for an older draft", "approval timeout")


def backfill(apps, schema_editor):
    """Best-effort `needed_review_at` for existing emails: the earliest sign that the email was
    in review (a review-routed triage, a blocked draft, a review alert), else `updated_at` for
    one in review now or whose reason names a review path. (A blocked draft almost always moved
    the email to review; one from a dashboard Re-draft counts as human involvement anyway.)"""
    Email = apps.get_model("inbox", "Email")
    Triage = apps.get_model("inbox", "Triage")
    Draft = apps.get_model("inbox", "Draft")
    Action = apps.get_model("inbox", "Action")
    first: dict[int, object] = {}

    def note(rows):
        for email_id, at in rows:
            if at is not None and (email_id not in first or at < first[email_id]):
                first[email_id] = at

    note(Triage.objects.filter(route="review").values("email_id").annotate(at=Min("created_at"))
         .values_list("email_id", "at"))
    note(Draft.objects.filter(ok=False).values("email_id").annotate(at=Min("created_at"))
         .values_list("email_id", "at"))
    note(Action.objects.filter(kind="slack_alert", idempotency_key__regex=r"^slack_alert:[0-9]+:review$")
         .values("email_id").annotate(at=Min("created_at")).values_list("email_id", "at"))
    in_review = models.Q(status="needs_review")
    for reason in REVIEW_REASONS:
        in_review |= models.Q(needs_review_reason__contains=reason)
    note((e.id, e.updated_at) for e in Email.objects.filter(in_review).exclude(id__in=list(first)).only("id", "updated_at"))
    for email_id, at in first.items():
        Email.objects.filter(pk=email_id, needed_review_at__isnull=True).update(needed_review_at=at)


class Migration(migrations.Migration):

    dependencies = [
        ("inbox", "0003_email_last_execution"),
    ]

    operations = [
        migrations.AddField(
            model_name="email",
            name="needed_review_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
