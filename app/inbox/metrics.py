"""Operational metrics for the dashboard and the daily Slack digest (WF5).

Definitions:
- handled: emails received in the period.
- automatic: replies sent whose draft was approved unchanged and that never went to review.
- reviewed: emails that went to review, or whose reply was edited by a person.
- time to reply: from Gmail receipt to the reply being sent (replies sent in the period).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from django.db.models import Count, Sum
from django.utils import timezone

from .models import Action, Approval, Category, Draft, Email, Failure, Status, Triage

PERIODS = {"today": 0, "7d": 7, "30d": 30}


def period_start(period: str, now: datetime | None = None) -> datetime:
    now = timezone.localtime(now or timezone.now())
    midnight = timezone.make_aware(datetime.combine(now.date(), time.min), now.tzinfo)
    return midnight - timedelta(days=PERIODS[period] - 1) if PERIODS[period] else midnight


def _minutes(delta: timedelta) -> float:
    return round(delta.total_seconds() / 60, 1)


def compute(period: str = "today", now: datetime | None = None) -> dict[str, Any]:
    start = period_start(period, now)
    emails = Email.objects.filter(received_at__gte=start)

    by_status = dict(emails.values_list("status").annotate(n=Count("id")))
    by_category = dict(emails.exclude(category=None).values_list("category").annotate(n=Count("id")))

    replies = Action.objects.filter(kind="reply_sent", ok=True, created_at__gte=start).select_related("email")
    reply_minutes = [_minutes(a.created_at - a.email.received_at) for a in replies]
    replied_ids = [a.email_id for a in replies]
    edited_ids = set(Approval.objects.filter(email_id__in=replied_ids, decision="edited").values_list("email_id", flat=True))
    reviewed_ids = set(Email.objects.filter(id__in=replied_ids).exclude(needs_review_reason=None)
                       .values_list("id", flat=True))
    automatic = len([i for i in replied_ids if i not in edited_ids and i not in reviewed_ids])

    ai_cost = (Triage.objects.filter(created_at__gte=start).aggregate(c=Sum("cost_usd"))["c"] or Decimal(0)) + (
        Draft.objects.filter(created_at__gte=start).aggregate(c=Sum("cost_usd"))["c"] or Decimal(0))
    handled = emails.count()

    return {
        "period": period,
        "since": start.isoformat(),
        "handled": handled,
        "by_status": by_status,
        "by_category": {c: by_category.get(c, 0) for c in Category.values},
        "replies_sent": len(replied_ids),
        "automatic": automatic,
        "reviewed": emails.exclude(needs_review_reason=None).count() + len(edited_ids - reviewed_ids),
        "rejected": by_status.get(Status.REJECTED, 0),
        "avg_minutes_to_reply": round(sum(reply_minutes) / len(reply_minutes), 1) if reply_minutes else None,
        "median_minutes_to_reply": round(median(reply_minutes), 1) if reply_minutes else None,
        "ai_cost_usd": str(ai_cost.quantize(Decimal("0.0001"))),
        "ai_cost_per_email_usd": str((ai_cost / handled).quantize(Decimal("0.0001"))) if handled else None,
        "injection_flagged": Triage.objects.filter(created_at__gte=start, injection_flag=True)
        .values("email_id").distinct().count(),
        "failures": Failure.objects.filter(created_at__gte=start).count(),
        # Current queues, not limited to the period.
        "open_failures": Failure.objects.filter(resolved=False).count(),
        "needs_review_now": Email.objects.filter(status=Status.NEEDS_REVIEW).count(),
        "awaiting_approval_now": Email.objects.filter(status=Status.AWAITING_APPROVAL).count(),
    }


def digest_text(m: dict[str, Any]) -> str:
    title = {"today": "Today", "7d": "Last 7 days", "30d": "Last 30 days"}[m["period"]]
    avg = f"{m['avg_minutes_to_reply']} min" if m["avg_minutes_to_reply"] is not None else "–"
    cats = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in m["by_category"].items() if v) or "none"
    lines = [
        f":bar_chart: *AI Ops Inbox · {title}*",
        f"Emails handled: *{m['handled']}* ({cats})",
        f"Replies sent: {m['replies_sent']} (fully automatic {m['automatic']}, reviewed or edited "
        f"{m['replies_sent'] - m['automatic']}) · rejected {m['rejected']}",
        f"Average time to reply: {avg}",
        f"AI cost: ${m['ai_cost_usd']}" + (f" (${m['ai_cost_per_email_usd']} per email)" if m["ai_cost_per_email_usd"] else ""),
        f"Injection attempts flagged: {m['injection_flagged']}",
        f"Failures: {m['failures']} ({m['open_failures']} still open)",
        f"Waiting now: {m['needs_review_now']} in review, {m['awaiting_approval_now']} awaiting approval",
    ]
    return "\n".join(lines)
