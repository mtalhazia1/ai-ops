"""Internal API used by n8n (Section 4). Every route needs the X-Internal-Token header."""

from __future__ import annotations

import hmac
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import NinjaAPI, Schema
from ninja.security import APIKeyHeader
from pydantic import ConfigDict, Field

from . import state
from .cleaning import clean_body
from .llm.triage import EmailInput, triage
from .models import Action, Email, Failure, Status, Triage


class InternalToken(APIKeyHeader):
    param_name = "X-Internal-Token"

    def authenticate(self, request, key):
        expected = settings.INTERNAL_TOKEN
        if expected and key and hmac.compare_digest(key, expected):
            return "n8n"
        return None


api = NinjaAPI(title="AI Ops Inbox internal API", auth=InternalToken(), urls_namespace="internal", docs_url=None)


@api.exception_handler(state.InvalidTransition)
def invalid_transition(request, exc: state.InvalidTransition):
    return api.create_response(
        request, {"detail": str(exc), "current": exc.current, "target": exc.target}, status=409
    )


# --- Health -------------------------------------------------------------------------

@api.get("/health")
def health(request):
    return {"ok": True, "emails": Email.objects.count()}


# --- Intake -------------------------------------------------------------------------

class AttachmentIn(Schema):
    filename: str = ""
    mime: str = ""
    size: int = 0
    gmail_attachment_id: str = ""


class EmailIn(Schema):
    model_config = ConfigDict(populate_by_name=True)

    gmail_message_id: str
    gmail_thread_id: str = ""
    # Raw "Name <address>" header values, as Gmail returns them. n8n sends "from".
    from_: str = Field("", alias="from")
    to: str = ""
    subject: str = ""
    date: Optional[str] = None
    text: str = ""
    html: str = ""
    attachments: list[AttachmentIn] = []


def _parse_date(value: Optional[str]) -> datetime:
    if not value:
        return timezone.now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return timezone.now()
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.utc)
    return parsed


@api.post("/emails")
def create_email(request, payload: EmailIn):
    """Insert-or-ignore on gmail_message_id. `created=false` tells n8n to stop."""
    existing = Email.objects.filter(gmail_message_id=payload.gmail_message_id).first()
    if existing:
        return {"id": existing.id, "created": False, "status": existing.status}

    from_name, from_email = parseaddr(payload.from_)
    _, to_email = parseaddr(payload.to)
    try:
        with transaction.atomic():
            email = Email.objects.create(
                gmail_message_id=payload.gmail_message_id,
                gmail_thread_id=payload.gmail_thread_id,
                from_email=from_email or payload.from_,
                from_name=from_name,
                to_email=to_email or payload.to,
                subject=payload.subject,
                body_text=payload.text,
                body_clean=clean_body(payload.text, payload.html),
                attachments=[a.dict() for a in payload.attachments],
                received_at=_parse_date(payload.date),
            )
    except IntegrityError:  # lost a race with a parallel execution
        email = Email.objects.get(gmail_message_id=payload.gmail_message_id)
        return {"id": email.id, "created": False, "status": email.status}
    return {"id": email.id, "created": True, "status": email.status}


@api.get("/emails/{email_id}")
def get_email(request, email_id: int):
    email = get_object_or_404(Email, pk=email_id)
    t = email.latest_triage
    return {
        "id": email.id,
        "status": email.status,
        "gmail_message_id": email.gmail_message_id,
        "gmail_thread_id": email.gmail_thread_id,
        "from_email": email.from_email,
        "from_name": email.from_name,
        "subject": email.subject,
        "category": email.category,
        "urgency": email.urgency,
        "attachments": email.attachments,
        "needs_review_reason": email.needs_review_reason,
        "fields": t.fields if t else {},
        "missing_fields": t.missing_fields if t else [],
        "dashboard_url": f"{settings.DASHBOARD_BASE_URL.rstrip('/')}/emails/{email.id}/",
    }


# --- Triage -------------------------------------------------------------------------

@api.post("/emails/{email_id}/triage")
def triage_email(request, email_id: int):
    email = get_object_or_404(Email, pk=email_id)
    if not state.can_transition(email.status, Status.TRIAGED):
        raise state.InvalidTransition(email.status, Status.TRIAGED)

    result = triage(
        EmailInput(
            from_email=email.from_email,
            from_name=email.from_name,
            subject=email.subject,
            body_clean=email.body_clean,
            attachments=email.attachments,
            received_at=email.received_at,
        )
    )
    Triage.objects.create(
        email=email,
        category=result.category,
        urgency=result.urgency,
        confidence=result.confidence,
        reason=result.reason,
        fields=result.fields,
        field_confidence=result.field_confidence,
        missing_fields=result.missing_fields,
        flags=result.flags,
        injection_flag=result.injection_flag,
        route=result.route,
        review_reason=result.review_reason,
        model_classify=result.model_classify,
        model_extract=result.model_extract,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
    )
    email.category, email.urgency = result.category, result.urgency
    email.save(update_fields=["category", "urgency", "updated_at"])
    target = Status.NEEDS_REVIEW if result.route == "review" else Status.TRIAGED
    state.transition(email, target, reason=result.review_reason or None)

    return {
        "id": email.id,
        "status": email.status,
        "category": result.category,
        "urgency": result.urgency,
        "confidence": result.confidence,
        "fields": result.fields,
        "missing_fields": result.missing_fields,
        "flags": result.flags,
        "injection_flag": result.injection_flag,
        "route": result.route,
        "review_reason": result.review_reason,
        "dashboard_url": f"{settings.DASHBOARD_BASE_URL.rstrip('/')}/emails/{email.id}/",
    }


# --- Status -------------------------------------------------------------------------

class StatusIn(Schema):
    status: Status
    reason: Optional[str] = None


@api.post("/emails/{email_id}/status")
def set_status(request, email_id: int, payload: StatusIn):
    email = get_object_or_404(Email, pk=email_id)
    state.transition(email, payload.status, reason=payload.reason)
    return {"id": email.id, "status": email.status}


# --- Actions (idempotency log) ------------------------------------------------------

_SECRET_KEYS = {"authorization", "token", "api_key", "apikey", "password", "secret", "x-internal-token"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if k.lower() in _SECRET_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class ActionIn(Schema):
    kind: str
    idempotency_key: str
    request: dict = {}
    response: dict = {}
    ok: bool = True


def _action_out(a: Action) -> dict:
    return {"id": a.id, "kind": a.kind, "idempotency_key": a.idempotency_key, "ok": a.ok, "response": a.response}


@api.get("/emails/{email_id}/actions")
def list_actions(request, email_id: int, key: Optional[str] = None):
    """`?key=` answers "was this already done?" before n8n repeats an outside call."""
    qs = Action.objects.filter(email_id=email_id)
    if key:
        qs = qs.filter(idempotency_key=key)
    actions = [_action_out(a) for a in qs.order_by("id")]
    return {"exists": bool(actions) if key else None, "actions": actions}


@api.post("/emails/{email_id}/actions")
def log_action(request, email_id: int, payload: ActionIn):
    email = get_object_or_404(Email, pk=email_id)
    valid_kinds = {k for k, _ in Action.KINDS}
    if payload.kind not in valid_kinds:
        return api.create_response(request, {"detail": f"unknown kind {payload.kind}"}, status=422)
    try:
        with transaction.atomic():
            action = Action.objects.create(
                email=email,
                kind=payload.kind,
                idempotency_key=payload.idempotency_key,
                request=_redact(payload.request),
                response=_redact(payload.response),
                ok=payload.ok,
            )
        created = True
    except IntegrityError:
        action = Action.objects.get(idempotency_key=payload.idempotency_key)
        created = False
    return {"created": created, **_action_out(action)}


# --- Failures (WF4) -----------------------------------------------------------------

class FailureIn(Schema):
    workflow: str = ""
    node: str = ""
    error: str = ""
    execution_id: str = ""
    email_id: Optional[int] = None


@api.post("/failures")
def record_failure(request, payload: FailureIn):
    email = Email.objects.filter(pk=payload.email_id).first() if payload.email_id else None
    failure = Failure.objects.create(
        email=email,
        workflow=payload.workflow[:255],
        node=payload.node[:255],
        error=payload.error,
        execution_id=payload.execution_id[:100],
    )
    if email and state.can_transition(email.status, Status.FAILED):
        state.transition(email, Status.FAILED, reason=f"{payload.workflow} failed at {payload.node}: {payload.error}"[:1000])
    return {"id": failure.id, "email_status": email.status if email else None}
