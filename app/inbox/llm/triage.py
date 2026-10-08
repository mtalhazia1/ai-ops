"""Classify + extract + validate one email (Sections 8.1, 8.2).

Pure function over an `EmailInput`, so the API endpoint and the evaluation runner
call exactly the same code.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.conf import settings

from . import guard, schemas
from .client import LLM, LLMError, Usage, get_llm

PROMPTS = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text()


@dataclass
class EmailInput:
    from_email: str
    subject: str
    body_clean: str
    from_name: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    received_at: datetime | None = None

    @property
    def attachment_names(self) -> list[str]:
        return [a.get("filename", "") for a in self.attachments if a.get("filename")]

    def haystack(self) -> str:
        return "\n".join([self.subject, self.body_clean, *self.attachment_names])


@dataclass
class TriageResult:
    category: str
    urgency: str
    confidence: float
    reason: str
    fields: dict[str, Any]
    field_confidence: dict[str, Any]
    missing_fields: list[str]
    flags: list[str]
    injection_flag: bool
    llm_injection_flag: bool
    injection_keywords: list[str]
    route: str
    review_reason: str
    model_classify: str
    model_extract: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cost_usd"] = str(self.cost_usd)
        return data


def render_email(email: EmailInput) -> str:
    sender = f"{email.from_name} <{email.from_email}>" if email.from_name else email.from_email
    attachments = ", ".join(email.attachment_names) or "(none)"
    body = email.body_clean[: settings.CLASSIFY_MAX_CHARS]
    received = email.received_at.date().isoformat() if email.received_at else "unknown"
    weekday = email.received_at.strftime("%A") if email.received_at else ""
    return (
        f"Received date: {received} {weekday}\n"
        "<email>\n"
        f"From: {sender}\n"
        f"Subject: {email.subject}\n"
        f"Attachments: {attachments}\n\n"
        f"{body}\n"
        "</email>"
    )


def classify(email: EmailInput, llm: LLM, company_name: str) -> tuple[schemas.Classification, Usage]:
    system = _prompt("classify.md").format(company_name=company_name)
    result = llm.structured(
        model=settings.MODEL_CLASSIFY,
        system=system,
        user=render_email(email),
        schema=schemas.CLASSIFY_SCHEMA,
        validator=schemas.Classification,
        max_tokens=1024,
    )
    return result.data, result.usage  # type: ignore[return-value]


def extract(email: EmailInput, category: str, llm: LLM, company_name: str):
    notes_file = PROMPTS / f"extract_{category}.md"
    system = _prompt("extract.md").format(
        company_name=company_name,
        category=category,
        category_notes=notes_file.read_text() if notes_file.exists() else "",
    )
    result = llm.structured(
        model=settings.MODEL_EXTRACT,
        system=system,
        user=render_email(email),
        schema=schemas.EXTRACTION_SCHEMAS[category],
        validator=schemas.EXTRACTION_MODELS[category],
        max_tokens=4096,
    )
    return result.data, result.usage


def _company_name() -> str:
    try:
        from ..models import Playbook

        return Playbook.get().company_name
    except Exception:  # no database (e.g. a dry run); the name is cosmetic here
        return "Indus Freight"


def triage(email: EmailInput, llm: LLM | None = None, company_name: str | None = None) -> TriageResult:
    llm = llm or get_llm()
    company_name = company_name or _company_name()
    started = time.monotonic()
    usage = Usage()
    review_reasons: list[str] = []

    keyword_hits = guard.injection_keywords(email.subject, email.body_clean)

    try:
        cls, u = classify(email, llm, company_name)
        usage.add(u)
    except LLMError as exc:
        if exc.usage:
            usage.add(exc.usage)
        return _result(
            email, usage, started, category="other", urgency="normal", confidence=0.0,
            reason="classification failed", route_reasons=[f"classification failed: {exc}"],
            keyword_hits=keyword_hits,
        )

    category, urgency = cls.category, cls.urgency
    if category == "claim":
        urgency = "high"

    fields: dict[str, Any] = {}
    field_confidence: dict[str, Any] = {}
    flags: list[str] = []
    missing: list[str] = []
    if category in schemas.FIELD_SPECS:
        try:
            extraction, u = extract(email, category, llm, company_name)
            usage.add(u)
            raw = extraction.to_record()
            fields, flags = guard.enforce_evidence(raw["fields"], raw["evidence"], email.haystack())
            field_confidence = {k: v for k, v in raw["field_confidence"].items() if fields.get(k) not in (None, [])}
            flags += guard.container_flags(fields)
            missing = schemas.required_missing(category, fields)
        except LLMError as exc:
            if exc.usage:
                usage.add(exc.usage)
            review_reasons.append(f"extraction failed: {exc}"[:300])

    if cls.confidence < settings.REVIEW_CONFIDENCE_THRESHOLD:
        review_reasons.append(f"low confidence ({cls.confidence:.2f})")
    if category == "other":
        review_reasons.append("category is other")
    if cls.contains_instructions_to_ai:
        review_reasons.append("classifier flagged instructions to AI")
    if keyword_hits:
        review_reasons.append(f"injection keywords: {', '.join(keyword_hits[:3])}")

    return _result(
        email, usage, started, category=category, urgency=urgency, confidence=cls.confidence,
        reason=cls.reason, route_reasons=review_reasons, keyword_hits=keyword_hits,
        llm_injection=cls.contains_instructions_to_ai, fields=fields, field_confidence=field_confidence,
        missing=missing, flags=flags,
    )


def _result(email, usage, started, *, category, urgency, confidence, reason, route_reasons, keyword_hits,
            llm_injection=False, fields=None, field_confidence=None, missing=None, flags=None) -> TriageResult:
    return TriageResult(
        category=category,
        urgency=urgency,
        confidence=confidence,
        reason=reason,
        fields=fields or {},
        field_confidence=field_confidence or {},
        missing_fields=missing or [],
        flags=flags or [],
        injection_flag=llm_injection or bool(keyword_hits),
        llm_injection_flag=llm_injection,
        injection_keywords=keyword_hits,
        route="review" if route_reasons else "auto",
        review_reason="; ".join(route_reasons),
        model_classify=settings.MODEL_CLASSIFY,
        model_extract=settings.MODEL_EXTRACT if category in schemas.FIELD_SPECS else "",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
        # Wall-clock time, including our own code, not just the API calls.
        latency_ms=int((time.monotonic() - started) * 1000),
    )
