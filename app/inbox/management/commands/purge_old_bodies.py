"""python manage.py purge_old_bodies [--days N] [--dry-run]

Deletes email bodies (and the drafted replies quoting them) older than
EMAIL_BODY_RETENTION_DAYS (default 90), keeping every row, status, category, cost and
action, so metrics and the audit trail stay intact (Section 12). Run it daily, e.g. from
cron: docker compose exec -T app python manage.py purge_old_bodies
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from inbox.models import Approval, Draft, Email

PURGED = "[deleted after retention period]"


class Command(BaseCommand):
    help = "Delete email bodies older than the retention period; keep metrics."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.EMAIL_BODY_RETENTION_DAYS)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=opts["days"])
        emails = Email.objects.filter(received_at__lt=cutoff).exclude(body_text=PURGED)
        count = emails.count()
        if not opts["dry_run"]:
            ids = list(emails.values_list("id", flat=True))
            Email.objects.filter(id__in=ids).update(body_text=PURGED, body_clean=PURGED)
            Draft.objects.filter(email_id__in=ids).update(body=PURGED)
            Approval.objects.filter(email_id__in=ids).update(final_body=PURGED)
        verb = "Would purge" if opts["dry_run"] else "Purged"
        self.stdout.write(f"{verb} bodies of {count} emails received before {cutoff:%Y-%m-%d}.")
