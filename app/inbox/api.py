"""Internal API used by n8n (Section 4). Every route needs the X-Internal-Token header."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import NinjaAPI, Schema
from ninja.security import APIKeyHeader
from pydantic import ConfigDict, Field

from . import process, state
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
    triage_row = email.latest_triage
    if triage_row is None:
        return {"id": email.id, "status": email.status, "gmail_message_id": email.gmail_message_id,
                "gmail_thread_id": email.gmail_thread_id, "from_email": email.from_email, "subject": email.subject,
                "dashboard_url": process.dashboard_url(email)}
    return process.payload(email, triage_row)


# --- Triage -------------------------------------------------------------------------

@api.post("/emails/{email_id}/triage")
def triage_email(request, email_id: int):
    get_object_or_404(Email, pk=email_id)
    # The row lock is held for the whole triage, so a parallel WF2 run for the same
    # email waits here and then takes the "re-run" path instead of paying for a
    # second LLM call.
    with transaction.atomic():
        email = Email.objects.select_for_update().get(pk=email_id)
        # A re-run of WF2 (retried execution, manual re-run) gets the saved triage back;
        # the idempotency keys stop repeated outside actions.
        if email.status in (Status.TRIAGED, Status.NEEDS_REVIEW) and email.latest_triage:
            return process.payload(email, email.latest_triage, rerun=True)
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
        triage_row = Triage.objects.create(
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

    return process.payload(email, triage_row)


# --- Status -------------------------------------------------------------------------

class StatusIn(Schema):
    status: Status
    reason: Optional[str] = None


@api.post("/emails/{email_id}/status")
def set_status(request, email_id: int, payload: StatusIn):
    email = get_object_or_404(Email, pk=email_id)
    if email.status == Status.FAILED and payload.status == Status.RECEIVED:  # Retry
        release_pending_claims(email)
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
    if not key:
        return {"exists": None, "done": None, "actions": actions}
    # `done` = it exists and succeeded; a failed attempt may be repeated.
    return {"exists": bool(actions), "done": bool(actions) and actions[0]["ok"],
            "response": actions[0]["response"] if actions else {}, "actions": actions}


class ClaimIn(Schema):
    kind: str
    idempotency_key: str


CLAIM_TTL = timedelta(minutes=10)


def release_pending_claims(email: Email) -> int:
    """A failed run never finishes its claimed actions; free them so a retry repeats them."""
    released = 0
    for action in Action.objects.filter(email=email, ok=False):
        if action.response.get("pending"):
            action.response = {"released": True, "claimed_at": action.response.get("claimed_at")}
            action.save(update_fields=["response"])
            released += 1
    return released


@api.post("/emails/{email_id}/actions/claim")
def claim_action(request, email_id: int, payload: ClaimIn):
    """Atomically reserve an idempotency key before an outside call.

    proceed=true: this run owns the key and must make the call, then log it.
    done=true: it already succeeded; `response` holds the saved result.
    Both false: another run holds a fresh claim; skip.
    A failed attempt, or a claim older than 10 minutes (a crashed run), can be re-claimed.
    """
    email = get_object_or_404(Email, pk=email_id)
    if payload.kind not in {k for k, _ in Action.KINDS}:
        return api.create_response(request, {"detail": f"unknown kind {payload.kind}"}, status=422)
    now = timezone.now()
    pending = {"pending": True, "claimed_at": now.isoformat()}
    try:
        with transaction.atomic():
            Action.objects.create(email=email, kind=payload.kind, idempotency_key=payload.idempotency_key,
                                  response=pending, ok=False)
        return {"proceed": True, "done": False, "response": {}}
    except IntegrityError:
        pass
    with transaction.atomic():
        action = Action.objects.select_for_update().get(idempotency_key=payload.idempotency_key)
        if action.ok:
            return {"proceed": False, "done": True, "response": action.response}
        claimed_at = action.response.get("claimed_at") if action.response.get("pending") else None
        if claimed_at and now - datetime.fromisoformat(claimed_at) < CLAIM_TTL:
            return {"proceed": False, "done": False, "response": {}}
        action.response, action.ok = pending, False
        action.save(update_fields=["response", "ok"])
    return {"proceed": True, "done": False, "response": {}}


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
        if not action.ok:  # an earlier attempt failed; record the new outcome
            action.request, action.response, action.ok = _redact(payload.request), _redact(payload.response), payload.ok
            action.save(update_fields=["request", "response", "ok"])
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
    if email:
        release_pending_claims(email)
    if email and state.can_transition(email.status, Status.FAILED):
        state.transition(email, Status.FAILED, reason=f"{payload.workflow} failed at {payload.node}: {payload.error}"[:1000])
    return {"id": failure.id, "email_status": email.status if email else None}
