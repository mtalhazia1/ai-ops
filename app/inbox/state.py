"""Allowed email status transitions (Section 13).

The internal API refuses anything not listed here with HTTP 409, so a duplicate
Slack click or a retried n8n execution can't move an email backwards or send twice.
"""

from django.db import transaction

from .models import Email, Status

TRANSITIONS: dict[str, set[str]] = {
    Status.RECEIVED: {Status.TRIAGED, Status.NEEDS_REVIEW, Status.FAILED},
    Status.TRIAGED: {Status.AWAITING_APPROVAL, Status.NEEDS_REVIEW, Status.FAILED},
    Status.AWAITING_APPROVAL: {Status.APPROVED, Status.REJECTED, Status.NEEDS_REVIEW, Status.FAILED},
    Status.NEEDS_REVIEW: {Status.APPROVED, Status.REJECTED, Status.IGNORED, Status.AWAITING_APPROVAL, Status.FAILED},
    Status.APPROVED: {Status.DONE, Status.FAILED},
    Status.FAILED: {Status.RECEIVED},
    Status.DONE: set(),
    Status.REJECTED: set(),
    Status.IGNORED: set(),
}


class InvalidTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"cannot move from {current} to {target}")
        self.current = current
        self.target = target


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def transition(email: Email, target: str, *, reason: str | None = None) -> Email:
    """Move an email to `target`, locking the row so concurrent calls serialize."""
    with transaction.atomic():
        locked = Email.objects.select_for_update().get(pk=email.pk)
        if not can_transition(locked.status, target):
            raise InvalidTransition(locked.status, target)
        locked.status = target
        update_fields = ["status", "updated_at"]
        if target == Status.NEEDS_REVIEW or target == Status.FAILED:
            locked.needs_review_reason = reason or locked.needs_review_reason
            update_fields.append("needs_review_reason")
        locked.save(update_fields=update_fields)
    email.refresh_from_db()
    return email
