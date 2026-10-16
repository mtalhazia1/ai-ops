"""Operations shared by the internal API and the dashboard: claims, retries and calls
into n8n webhooks."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import state
from .llm.draft import DraftInput, PlaybookData, draft_reply, reply_subject
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
    otherwise start WF2 again (it reuses the saved triage). Returns which one ran.

    If n8n can't be reached the email stays retryable: in send mode it is still `failed`
    (WF3 moves it to `approved`); in process mode it is moved back from `received` to
    `failed`. (n8n is called after the commit, not inside the transaction, so WF2 never
    reads the email before it is `received`.)"""
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
        try:
            call_n8n(settings.N8N_PROCESS_WEBHOOK_URL, {"email_id": email.id})
        except Exception:
            _back_to_failed(email)
            raise
        mode = "process"
    Failure.objects.filter(email=email, resolved=False).update(resolved=True, retried_at=timezone.now())
    return mode


def _back_to_failed(email: Email) -> None:
    """Undo Retry's failed → received after n8n couldn't be called, unless a WF2 run did
    start after all and has already moved the email on."""
    with transaction.atomic():
        locked = Email.objects.select_for_update().get(pk=email.pk)
        if locked.status == Status.RECEIVED:
            state.transition(locked, Status.FAILED)
    email.refresh_from_db()


# The statuses ShipMatch documents as a refusal (Section 10). Anything else that isn't
# 200/201 (429, 5xx, no answer) is transient: WF2 fails the run so Retry repeats the upload.
UPLOAD_REFUSALS = {400: "file rejected", 402: "uploads paused on the ShipMatch plan"}
UPLOAD_REFUSED_PREFIX = "ShipMatch did not accept: "
CLAIM_TTL = timedelta(minutes=10)


class UploadsNotFinished(Exception):
    """An attachment's ShipMatch upload has no final answer yet, so a "documents received"
    reply can't be written. `in_progress`: another run holds a fresh claim and drafts once
    it is done; otherwise the upload failed or was abandoned and the run should fail (Retry)."""

    def __init__(self, message: str, *, in_progress: bool):
        super().__init__(message)
        self.in_progress = in_progress


def _attachment_name(email: Email, action: Action) -> str:
    name = (action.request or {}).get("filename")
    if name:
        return name
    attachment_id = action.idempotency_key.rsplit(":", 1)[-1]
    for a in email.attachments or []:
        if a.get("gmail_attachment_id") == attachment_id and a.get("filename"):
            return a["filename"]
    return "an attachment"


def upload_problems(email: Email) -> tuple[list[str], list[str], list[str]]:
    """ShipMatch uploads without a successful result, as readable lines in a fixed order:
    (refused by ShipMatch, in progress in another run, failed or abandoned)."""
    refused, in_progress, unfinished = [], [], []
    now = timezone.now()
    for action in email.actions.filter(kind="shipmatch_upload", ok=False).order_by("created_at", "id"):
        response = action.response or {}
        name = _attachment_name(email, action)
        if response.get("pending"):
            claimed_at = response.get("claimed_at")
            if claimed_at and now - datetime.fromisoformat(claimed_at) < CLAIM_TTL:
                in_progress.append(f"{name}: upload in progress")
            else:
                unfinished.append(f"{name}: upload never finished")
        elif response.get("released"):
            unfinished.append(f"{name}: upload not repeated after a failed run")
        elif response.get("status") in UPLOAD_REFUSALS:
            detail = UPLOAD_REFUSALS[response["status"]]
            if response.get("detail"):
                detail += f" ({response['detail']})"
            refused.append(f"{name}: {detail}")
        else:
            status = response.get("status")
            unfinished.append(f"{name}: upload failed ({f'HTTP {status}' if status else 'no response recorded'})")
    return refused, in_progress, unfinished


def lookups(email: Email) -> list[dict]:
    latest = email.actions.filter(kind="shipmatch_lookup", ok=True).order_by("-created_at", "-id").first()
    return (latest.response or {}).get("results", []) if latest else []


def create_draft(email: Email) -> Draft:
    """Draft a reply from the latest triage and the playbook; saves and returns the Draft.
    Status changes are the caller's business.

    Every draft path (WF2's /draft, the dashboard's Re-draft) comes through here, so the
    ShipMatch upload guard applies to all of them:
    - an upload ShipMatch refused: no AI call; a blocked Draft with the reason is saved, so
      the reviewer writes the reply instead of approving "documents received";
    - an upload in flight, failed or abandoned: raises UploadsNotFinished.
    """
    refused, in_progress, unfinished = upload_problems(email)
    if refused:
        return Draft.objects.create(email=email, subject=reply_subject(email.subject), body="", ok=False,
                                    blocked_reason=(UPLOAD_REFUSED_PREFIX + "; ".join(refused))[:1000])
    if in_progress or unfinished:
        raise UploadsNotFinished("ShipMatch upload not finished: " + "; ".join(unfinished + in_progress),
                                 in_progress=not unfinished)
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
