"""What WF2 needs to act on a triaged email, prepared in code (Section 9, WF2 step 3).

n8n makes the outside calls; this module decides their content, so it is tested with
pytest and n8n expressions stay one-liners. Everything sent to HubSpot that identifies
the contact comes from Gmail metadata, never from LLM output (Section 12).
"""

from __future__ import annotations

import html
from pathlib import PurePath
from typing import Any

from django.conf import settings

from .models import Email, Triage

# ShipMatch accepts these (Section 10).
SHIPMATCH_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".xlsx", ".csv", ".zip"}

FIELD_LABELS = {
    "company": "Company",
    "contact_name": "Contact",
    "contact_email": "Email",
    "contact_phone": "Phone",
    "origin_city": "Origin",
    "destination_city": "Destination",
    "pickup_date": "Pickup date",
    "mode": "Mode",
    "weight_kg": "Weight (kg)",
    "pieces": "Pieces",
    "package_type": "Package type",
    "commodity": "Commodity",
    "hazardous": "Hazardous",
    "special_requirements": "Special requirements",
    "quote_reference": "Quote reference",
    "pickup_address": "Pickup address",
    "delivery_address": "Delivery address",
    "reference_numbers": "References",
}


def dashboard_url(email: Email) -> str:
    return f"{settings.DASHBOARD_BASE_URL.rstrip('/')}/emails/{email.id}/"


def slack_escape(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_name(email: Email) -> tuple[str, str]:
    name = (email.from_name or "").strip().strip('"')
    if not name:
        return "", ""
    first, _, last = name.partition(" ")
    return first, last


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _filled(fields: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(k, v) for k, v in fields.items() if v not in (None, [], "")]


def deal_name(email: Email, fields: dict[str, Any]) -> str:
    company = fields.get("company") or email.from_name or email.from_email.split("@")[-1]
    origin = fields.get("origin_city") or "?"
    destination = fields.get("destination_city") or "?"
    return f"{company} {origin}→{destination}"[:200]


def note_body(email: Email, triage: Triage) -> str:
    rows = "".join(
        f"<li><b>{html.escape(FIELD_LABELS.get(k, k))}:</b> {html.escape(_fmt(v))}</li>" for k, v in _filled(triage.fields)
    )
    missing = ", ".join(FIELD_LABELS.get(m, m) for m in triage.missing_fields) or "none"
    return (
        f"<p><b>AI Ops Inbox</b>: {html.escape(triage.category)} from {html.escape(email.from_email)}</p>"
        f"<p>Subject: {html.escape(email.subject)}</p>"
        f"<ul>{rows}</ul>"
        f"<p>Missing: {html.escape(missing)}</p>"
        f'<p><a href="{html.escape(dashboard_url(email))}">Open in AI Ops Inbox</a></p>'
    )


def lookup_refs(fields: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for kind, name in (("container", "container_numbers"), ("bl", "bl_numbers"), ("po", "po_numbers")):
        for value in fields.get(name) or []:
            ref = "".join(str(value).split())
            if ref and ref.upper() not in seen:
                seen.add(ref.upper())
                refs.append({"kind": kind, "ref": ref})
    booking = fields.get("booking_reference")
    if booking and str(booking).upper() not in seen:
        refs.append({"kind": "booking", "ref": str(booking).strip()})
    return refs


def upload_attachments(email: Email) -> list[dict[str, Any]]:
    return [
        a
        for a in email.attachments
        if a.get("gmail_attachment_id") and PurePath(a.get("filename", "")).suffix.lower() in SHIPMATCH_EXTENSIONS
    ]


def review_text(email: Email, reason: str) -> str:
    return (
        f":inbox_tray: *Needs review*: {slack_escape(email.subject or '(no subject)')}\n"
        f"From: {slack_escape(email.from_name or '')} {slack_escape(email.from_email)}\n"
        f"Reason: {slack_escape(reason)}\n"
        f"<{dashboard_url(email)}|Open in dashboard>"
    )


def claim_text(email: Email, triage: Triage) -> str:
    f = triage.fields
    lines = [
        f":rotating_light: *Claim*: {slack_escape(email.subject or '(no subject)')}",
        f"From: {slack_escape(email.from_name or '')} {slack_escape(email.from_email)}",
    ]
    if f.get("reference_numbers"):
        lines.append(f"References: {slack_escape(_fmt(f['reference_numbers']))}")
    if f.get("damage_description"):
        lines.append(f"Damage: {slack_escape(f['damage_description'])}")
    if f.get("pieces_affected") is not None:
        lines.append(f"Pieces affected: {slack_escape(f['pieces_affected'])}")
    if f.get("estimated_value") is not None:
        lines.append(f"Estimated value: {slack_escape(f['estimated_value'])} {slack_escape(f.get('currency') or '')}".rstrip())
    lines.append(f"Photos attached: {'yes' if f.get('photos_attached') else 'no'}")
    lines.append(f"<{dashboard_url(email)}|Open in dashboard>")
    return "\n".join(lines)


ACTION_LABELS = {
    "hubspot_contact": "HubSpot contact",
    "hubspot_deal": "HubSpot deal",
    "hubspot_task": "HubSpot task",
    "hubspot_note": "HubSpot note",
    "shipmatch_upload": "ShipMatch upload",
    "shipmatch_lookup": "ShipMatch lookup",
    "slack_alert": "Slack alert",
}


def approval_text(email: Email, triage: Triage, draft) -> str:
    """The Slack approval message (mrkdwn): what came in, what was done, the draft."""
    fields = " · ".join(
        f"{FIELD_LABELS.get(k, k.replace('_', ' ').capitalize())}: {slack_escape(_fmt(v))}" for k, v in _filled(triage.fields)
    )
    missing = ", ".join(FIELD_LABELS.get(m, m) for m in triage.missing_fields)
    done = sorted({ACTION_LABELS.get(a.kind, a.kind) for a in email.actions.filter(ok=True)})
    quoted = "\n".join("> " + slack_escape(line) if line.strip() else ">" for line in draft.body.splitlines())
    lines = [
        f":envelope_with_arrow: *Approval needed* · {triage.category} · {triage.urgency} urgency",
        f"From: {slack_escape(email.from_name or '')} {slack_escape(email.from_email)}",
        f"Subject: {slack_escape(email.subject or '(no subject)')}",
    ]
    if fields:
        lines.append(f"Fields: {fields}")
    if missing:
        lines.append(f"Missing: {slack_escape(missing)}")
    lines.append(f"Done so far: {', '.join(done) if done else 'nothing yet'}")
    lines.append(f"*Draft reply* ({slack_escape(draft.subject)}):")
    lines.append(quoted)
    return "\n".join(lines)[:2900]  # Slack section text limit is 3000


def payload(email: Email, triage: Triage, *, rerun: bool = False) -> dict[str, Any]:
    """The triage response WF2 works from."""
    first, last = _split_name(email)
    fields = triage.fields
    return {
        "id": email.id,
        "status": email.status,
        "rerun": rerun,
        "gmail_message_id": email.gmail_message_id,
        "gmail_thread_id": email.gmail_thread_id,
        "from_email": email.from_email,
        "from_name": email.from_name,
        "subject": email.subject,
        "category": triage.category,
        "urgency": triage.urgency,
        "confidence": triage.confidence,
        "fields": fields,
        "missing_fields": triage.missing_fields,
        "flags": triage.flags,
        "injection_flag": triage.injection_flag,
        "route": triage.route,
        "review_reason": triage.review_reason,
        "needs_review_reason": email.needs_review_reason,
        "dashboard_url": dashboard_url(email),
        "review_text": review_text(email, triage.review_reason or "manual review"),
        "shipmatch_enabled": settings.SHIPMATCH_ENABLED,
        "crm": {
            "contact": {"email": email.from_email, "firstname": first, "lastname": last},
            "deal_name": deal_name(email, fields),
            "deal_stage": "info_requested" if triage.missing_fields else "new_request",
            "note_body": note_body(email, triage),
            "task_subject": f"Confirm booking {fields.get('quote_reference') or ', '.join(fields.get('reference_numbers') or []) or email.subject}"[:250],
            "task_body": note_body(email, triage),
            "task_priority": "HIGH" if triage.urgency == "high" else "MEDIUM",
        },
        "lookup_refs": lookup_refs(fields),
        "upload_attachments": upload_attachments(email),
        "slack": {
            "review_text": review_text(email, triage.review_reason or "manual review"),
            "claim_text": claim_text(email, triage) if triage.category == "claim" else "",
            "paperwork_review_text": review_text(
                email, "ShipMatch is switched off or no uploadable attachments: handle the paperwork manually"
            ),
        },
    }
