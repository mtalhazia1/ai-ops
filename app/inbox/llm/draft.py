"""Reply drafting (Section 8.3).

The model sees structured data only (category, fields, missing fields, lookups), not
the raw email, which keeps instructions hidden in an email away from the drafter. The
subject and recipient are set in code. After the model, `guard.check_reply` blocks
new addresses or links, money amounts and over-long replies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from pydantic import BaseModel, ConfigDict

from . import guard
from .client import LLM, LLMError, Usage, get_llm
from .triage import _prompt

FIELD_LABELS = {
    "origin_city": "origin city",
    "destination_city": "destination city",
    "pickup_date": "pickup date",
    "weight_kg": "total weight (kg) or number of pieces",
    "quote_reference": "quote reference",
    "reference_number": "container, B/L or PO number",
    "reference_numbers": "shipment or B/L reference",
}


class DraftOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str
    asks_for: list[str]


DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string", "description": "The full reply text including the signature."},
        "asks_for": {"type": "array", "items": {"type": "string"}, "description": "Missing details the reply asks for."},
    },
    "required": ["body", "asks_for"],
    "additionalProperties": False,
}


@dataclass
class PlaybookData:
    company_name: str
    signature: str
    tone: str = ""
    facts: str = ""
    never_promise: str = ""

    @classmethod
    def from_model(cls, playbook) -> "PlaybookData":
        return cls(playbook.company_name, playbook.signature, playbook.tone, playbook.facts, playbook.never_promise)

    def text(self) -> str:
        return "\n".join([self.company_name, self.signature, self.facts, self.never_promise])


@dataclass
class DraftInput:
    category: str
    fields: dict[str, Any]
    missing_fields: list[str]
    subject: str
    from_name: str = ""
    lookups: list[dict[str, Any]] = field(default_factory=list)
    lookups_available: bool = True
    # Text the reply may quote addresses/links from: the original email.
    source_text: str = ""


@dataclass
class DraftResult:
    ok: bool
    subject: str
    body: str
    asks_for: list[str]
    problems: list[str]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0

    @property
    def blocked_reason(self) -> str:
        return "; ".join(self.problems)


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if re.match(r"^re\s*:", subject, re.IGNORECASE) else f"Re: {subject or 'your email'}"


def _first_name(name: str) -> str:
    name = (name or "").strip().strip('"')
    return name.split()[0] if name else ""


def render_input(data: DraftInput) -> str:
    payload = {
        "category": data.category,
        "sender_first_name": _first_name(data.from_name) or None,
        "original_subject": data.subject,
        "fields": {k: v for k, v in data.fields.items() if v not in (None, [], "")},
        "missing_fields": [FIELD_LABELS.get(m, m.replace("_", " ")) for m in data.missing_fields],
        "lookup_results": data.lookups if data.lookups_available else "unavailable",
    }
    return "<data>\n" + json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n</data>"


def _ensure_signature(body: str, signature: str) -> str:
    sig = signature.strip()
    first_line = sig.splitlines()[0].strip() if sig else ""
    if first_line and first_line.lower() not in body.lower():
        body = body.rstrip() + "\n\n" + sig
    return body


def draft_reply(data: DraftInput, playbook: PlaybookData, llm: LLM | None = None) -> DraftResult:
    llm = llm or get_llm()
    system = _prompt("draft.md").format(
        company_name=playbook.company_name,
        signature=playbook.signature,
        tone=playbook.tone or "Friendly, brief and professional.",
        facts=playbook.facts or "(none)",
        never_promise=playbook.never_promise or "(none)",
    )
    subject = reply_subject(data.subject)
    try:
        result = llm.structured(
            model=settings.MODEL_DRAFT, system=system, user=render_input(data), schema=DRAFT_SCHEMA,
            validator=DraftOutput, max_tokens=2048,
        )
    except LLMError as exc:
        usage = exc.usage or Usage()
        return DraftResult(False, subject, "", [], [f"drafting failed: {exc}"[:300]], settings.MODEL_DRAFT,
                           usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.latency_ms)
    out: DraftOutput = result.data  # type: ignore[assignment]
    body = _ensure_signature(out.body.strip(), playbook.signature)
    problems = guard.check_reply(body, allowed_sources=[data.source_text, playbook.text()])
    usage = result.usage
    return DraftResult(not problems, subject, body, out.asks_for, problems, settings.MODEL_DRAFT,
                       usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.latency_ms)
