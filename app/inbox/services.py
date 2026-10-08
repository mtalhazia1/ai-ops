"""Operations shared by the internal API and the dashboard: claims, retries and calls
into n8n webhooks."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import state
from .llm.draft import DraftInput, PlaybookData, draft_reply
from .models import Action, Draft, Email, Failure, Playbook, Status

logger = logging.getLogger(__name__)


class N8nError(Exception):
    pass


def release_pending_claims(email: Email) -> int:
    """A failed run never finishes its claimed actions; free them so a retry repeats them."""
    released = 0
    for action in Action.objects.filter(email=email, ok=False):
        if action.response.get("pending"):
            action.response = {"released": True, "claimed_at": action.response.get("claimed_at")}
            action.save(update_fields=["response"])
            released += 1
    return released


def call_n8n(url: str, payload: dict) -> None:
    """POST to an n8n webhook with the shared secret header."""
    if not url:
        raise N8nError("n8n webhook URL is not configured")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-webhook-secret": settings.N8N_WEBHOOK_SECRET},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise N8nError(f"n8n answered {resp.status}")
    except urllib.error.HTTPError as exc:
        raise N8nError(f"n8n answered {exc.code}: {exc.read()[:200]!r}") from exc
    except urllib.error.URLError as exc:
        raise N8nError(f"n8n unreachable: {exc.reason}") from exc


def send_decision(email: Email, *, decision: str, reviewer: str, final_body: str = "") -> None:
    """Dashboard approve/edit/reject: WF3 records it through the internal API and sends."""
    call_n8n(settings.N8N_EXECUTE_WEBHOOK_URL, {
        "email_id": email.id, "decision": decision, "reviewer": reviewer, "channel": "dashboard",
        "final_body": final_body,
    })


def retry(email: Email) -> str:
    """Retry a failed email. With a decision already made, resume at sending (WF3);
    otherwise start WF2 again (it reuses the saved triage). Returns which one ran."""
    latest = email.approvals.order_by("-decided_at", "-id").first()
    if latest and latest.decision != "rejected":
        with transaction.atomic():
            release_pending_claims(email)
        call_n8n(settings.N8N_EXECUTE_WEBHOOK_URL, {
            "email_id": email.id, "decision": "approved", "reviewer": latest.reviewer,
            "channel": latest.channel, "final_body": "",
        })
        mode = "send"
    else:
        with transaction.atomic():
            release_pending_claims(email)
            state.transition(email, Status.RECEIVED)
        call_n8n(settings.N8N_PROCESS_WEBHOOK_URL, {"email_id": email.id})
        mode = "process"
    Failure.objects.filter(email=email, resolved=False).update(resolved=True, retried_at=timezone.now())
    return mode


def lookups(email: Email) -> list[dict]:
    latest = email.actions.filter(kind="shipmatch_lookup", ok=True).order_by("-created_at", "-id").first()
    return (latest.response or {}).get("results", []) if latest else []


def create_draft(email: Email) -> Draft:
    """Draft a reply from the latest triage and the playbook; saves and returns the Draft.
    Status changes are the caller's business."""
    triage_row = email.latest_triage
    result = draft_reply(
        DraftInput(
            category=triage_row.category,
            fields=triage_row.fields,
            missing_fields=triage_row.missing_fields,
            subject=email.subject,
            from_name=email.from_name,
            lookups=lookups(email),
            lookups_available=settings.SHIPMATCH_ENABLED,
            source_text="\n".join([email.subject, email.body_text, email.from_email, email.reply_to]),
        ),
        PlaybookData.from_model(Playbook.get()),
    )
    return Draft.objects.create(
        email=email, subject=result.subject, body=result.body, asks_for=result.asks_for, ok=result.ok,
        blocked_reason=result.blocked_reason, model=result.model, input_tokens=result.input_tokens,
        output_tokens=result.output_tokens, cost_usd=result.cost_usd, latency_ms=result.latency_ms,
    )
