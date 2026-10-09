"""JSON schemas (sent to the API as structured output) and pydantic models (used to
validate what comes back), generated from one field spec per category (Section 8)."""

from datetime import date
from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, create_model, model_validator

CATEGORIES = ["quote_request", "booking", "shipment_status", "paperwork", "claim", "other"]
URGENCIES = ["low", "normal", "high"]
MODES = ["FTL", "LTL", "FCL", "LCL", "air", "courier"]


# --- Classification -----------------------------------------------------------------

class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal["quote_request", "booking", "shipment_status", "paperwork", "claim", "other"]
    urgency: Literal["low", "normal", "high"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    contains_instructions_to_ai: bool


CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "urgency": {"type": "string", "enum": URGENCIES},
        "confidence": {"type": "number", "description": "0.0 to 1.0"},
        "reason": {"type": "string", "description": "One sentence."},
        "contains_instructions_to_ai": {"type": "boolean"},
    },
    "required": ["category", "urgency", "confidence", "reason", "contains_instructions_to_ai"],
    "additionalProperties": False,
}


# --- Extraction ---------------------------------------------------------------------
# Field kinds: "str", "date", "num", "int", "bool", "list", or a list of enum values.

FIELD_SPECS: dict[str, dict[str, Any]] = {
    "quote_request": {
        "company": "str",
        "contact_name": "str",
        "contact_email": "str",
        "contact_phone": "str",
        "origin_city": "str",
        "destination_city": "str",
        "pickup_date": "date",
        "mode": MODES,
        "weight_kg": "num",
        "pieces": "int",
        "package_type": "str",
        "commodity": "str",
        "hazardous": "bool",
        "special_requirements": "list",
    },
    "booking": {
        "quote_reference": "str",
        "company": "str",
        "contact_name": "str",
        "pickup_date": "date",
        "pickup_address": "str",
        "delivery_address": "str",
        "reference_numbers": "list",
    },
    "shipment_status": {
        "container_numbers": "list",
        "bl_numbers": "list",
        "po_numbers": "list",
        "booking_reference": "str",
    },
    "paperwork": {
        "document_types_mentioned": "list",
        "reference_numbers": "list",
    },
    "claim": {
        "reference_numbers": "list",
        "damage_description": "str",
        "pieces_affected": "int",
        "photos_attached": "bool",
        "estimated_value": "num",
        "currency": "str",
    },
}

_PY_TYPES = {"str": str, "date": date, "num": float, "int": int, "bool": bool}
_EMPTY_VALUES = {"", "null", "none", "n/a", "na", "unknown", "-"}


def extraction_schema(category: str) -> dict[str, Any]:
    """A flat list of {field, value, evidence, confidence} items.

    Deliberately free of nullable/union types: the API allows at most 16 union-typed
    parameters per request, and a nullable-object design needs ~40. Fields the email
    doesn't state are simply absent; list fields repeat one item per value.
    """
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": list(FIELD_SPECS[category])},
                        "value": {"type": "string"},
                        "evidence": {"type": "string", "description": "Exact quote from the email."},
                        "confidence": {"type": "number", "description": "0.0 to 1.0"},
                    },
                    "required": ["field", "value", "evidence", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _build_fields_model(category: str) -> type[BaseModel]:
    definitions: dict[str, Any] = {}
    for name, kind in FIELD_SPECS[category].items():
        if isinstance(kind, list):
            definitions[name] = (Optional[Literal[tuple(kind)]], None)  # type: ignore[valid-type]
        elif kind == "list":
            definitions[name] = (list[str], Field(default_factory=list))
        else:
            definitions[name] = (Optional[_PY_TYPES[kind]], None)
    return create_model(f"{category}_fields", __config__=ConfigDict(extra="forbid"), **definitions)


class ExtractionBase(BaseModel):
    """Validates the raw items and converts them into typed fields.

    Conversion errors (e.g. a date that isn't ISO) raise ValidationError, so the LLM
    client retries once with the error message.
    """

    model_config = ConfigDict(extra="forbid")
    category: ClassVar[str] = ""
    fields_model: ClassVar[type[BaseModel]]
    items: list[Any]
    _record: dict[str, Any] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _convert(self):
        spec = FIELD_SPECS[self.category]
        best: dict[str, Any] = {}
        values: dict[str, Any] = {}
        evidence: dict[str, Any] = {}
        confidence: dict[str, Any] = {}
        for item in self.items:
            raw = item.value.strip()
            if raw.lower() in _EMPTY_VALUES:
                continue
            kind = spec[item.field]
            if kind in ("num", "int"):
                raw = raw.replace(",", "")
            if kind == "list":
                values.setdefault(item.field, [])
                if raw not in values[item.field]:
                    values[item.field].append(raw)
                    evidence[item.field] = " | ".join(filter(None, [evidence.get(item.field), item.evidence]))
                    confidence[item.field] = min(confidence.get(item.field, 1.0), item.confidence)
            elif item.field not in best or item.confidence > best[item.field]:
                best[item.field] = item.confidence
                values[item.field] = raw
                evidence[item.field] = item.evidence
                confidence[item.field] = item.confidence
        typed = self.fields_model.model_validate(values)
        self._record = {
            "fields": typed.model_dump(mode="json"),
            "evidence": evidence,
            "field_confidence": confidence,
        }
        return self

    def to_record(self) -> dict[str, Any]:
        return self._record


def _build_extraction_model(category: str) -> type[BaseModel]:
    item = create_model(
        f"{category}_item",
        __config__=ConfigDict(extra="forbid"),
        field=(Literal[tuple(FIELD_SPECS[category])], ...),  # type: ignore[valid-type]
        value=(str, ...),
        evidence=(str, ""),
        confidence=(float, Field(ge=0.0, le=1.0)),
    )
    model = create_model(f"{category}_extraction", __base__=ExtractionBase, items=(list[item], ...))  # type: ignore[valid-type]
    model.category = category
    model.fields_model = _build_fields_model(category)
    return model


EXTRACTION_MODELS: dict[str, type[ExtractionBase]] = {c: _build_extraction_model(c) for c in FIELD_SPECS}
EXTRACTION_SCHEMAS: dict[str, dict[str, Any]] = {c: extraction_schema(c) for c in FIELD_SPECS}


def required_missing(category: str, fields: dict[str, Any]) -> list[str]:
    """Required fields that are still empty. The reply draft asks for these."""

    def empty(name: str) -> bool:
        value = fields.get(name)
        return value is None or value == [] or value == ""

    missing: list[str] = []
    if category == "quote_request":
        missing += [n for n in ("origin_city", "destination_city", "pickup_date") if empty(n)]
        if empty("weight_kg") and empty("pieces"):
            missing.append("weight_kg")
    elif category == "booking":
        if empty("quote_reference") and empty("reference_numbers"):
            missing.append("quote_reference")
        if empty("pickup_date"):
            missing.append("pickup_date")
    elif category == "shipment_status":
        if all(empty(n) for n in ("container_numbers", "bl_numbers", "po_numbers", "booking_reference")):
            missing.append("reference_number")
    elif category == "claim":
        if empty("reference_numbers"):
            missing.append("reference_numbers")
    return missing
