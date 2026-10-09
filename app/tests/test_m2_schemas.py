import pytest
from pydantic import ValidationError

from inbox.llm import schemas


def _walk_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _walk_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _walk_objects(value)


@pytest.mark.parametrize("category", list(schemas.FIELD_SPECS))
def test_extraction_schema_is_strict(category):
    schema = schemas.EXTRACTION_SCHEMAS[category]
    for obj in _walk_objects(schema):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_classification_rejects_unknown_category():
    with pytest.raises(ValidationError):
        schemas.Classification.model_validate(
            {"category": "spam", "urgency": "low", "confidence": 0.5, "reason": "", "contains_instructions_to_ai": False}
        )


def _count_unions(schema):
    count = 0
    if isinstance(schema, dict):
        if "anyOf" in schema or isinstance(schema.get("type"), list):
            count += 1
        count += sum(_count_unions(v) for v in schema.values())
    elif isinstance(schema, list):
        count += sum(_count_unions(v) for v in schema)
    return count


@pytest.mark.parametrize("schema", [schemas.CLASSIFY_SCHEMA, *schemas.EXTRACTION_SCHEMAS.values()])
def test_schemas_within_api_limits(schema):
    # API limits: 16 union-typed params, 24 optional params per request.
    assert _count_unions(schema) <= 16
    assert all(set(o["required"]) == set(o["properties"]) for o in _walk_objects(schema))


def _items(*items):
    return {"items": [{"field": f, "value": v, "evidence": e, "confidence": c} for f, v, e, c in items]}


def test_extraction_converts_items_to_typed_fields():
    model = schemas.EXTRACTION_MODELS["quote_request"]
    record = model.model_validate(_items(
        ("pickup_date", "2026-10-13", "next Tuesday", 0.8),
        ("mode", "LTL", "part load", 0.7),
        ("weight_kg", "1,200", "1200kg", 0.9),
        ("hazardous", "false", "non-hazardous", 0.9),
        ("special_requirements", "tail lift", "tail lift", 0.9),
        ("special_requirements", "insurance", "insured", 0.6),
        ("origin_city", "null", "", 0.1),
        ("destination_city", "Karachi", "Khi", 0.5),
        ("destination_city", "Karachi", "to Karachi", 0.9),
    )).to_record()
    f = record["fields"]
    assert f["pickup_date"] == "2026-10-13" and f["mode"] == "LTL"
    assert f["weight_kg"] == 1200.0 and f["hazardous"] is False
    assert f["special_requirements"] == ["tail lift", "insurance"]
    assert f["origin_city"] is None and f["pieces"] is None
    assert record["evidence"]["destination_city"] == "to Karachi"  # highest confidence wins
    assert record["field_confidence"]["special_requirements"] == 0.6


@pytest.mark.parametrize("item", [("mode", "boat", "x", 0.5), ("pickup_date", "next tuesday", "x", 0.5),
                                  ("weight_kg", "heavy", "x", 0.5), ("colour", "red", "x", 0.5)])
def test_extraction_rejects_bad_values(item):
    with pytest.raises(ValidationError):
        schemas.EXTRACTION_MODELS["quote_request"].model_validate(_items(item))


def test_required_missing_quote():
    assert schemas.required_missing("quote_request", {"origin_city": "Lahore", "pieces": 4}) == [
        "destination_city",
        "pickup_date",
    ]
    assert "weight_kg" in schemas.required_missing("quote_request", {})


def test_required_missing_status():
    assert schemas.required_missing("shipment_status", {"container_numbers": []}) == ["reference_number"]
    assert schemas.required_missing("shipment_status", {"po_numbers": ["7781"]}) == []
