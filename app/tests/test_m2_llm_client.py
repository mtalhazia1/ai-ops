import json
from decimal import Decimal

import pytest

from inbox.llm import schemas
from inbox.llm.client import AnthropicLLM, LLMError, LLMValidationError, cost_usd

from .conftest import fake_anthropic

GOOD = {"category": "claim", "urgency": "high", "confidence": 0.9, "reason": "damage", "contains_instructions_to_ai": False}


def _call(llm, model="claude-sonnet-5-5"):
    return llm.structured(model=model, system="s", user="u", schema=schemas.CLASSIFY_SCHEMA, validator=schemas.Classification)


def test_valid_output_first_try():
    client, calls = fake_anthropic(json.dumps(GOOD))
    result = _call(AnthropicLLM(client))
    assert result.data.category == "claim"
    assert len(calls) == 1
    assert calls[0]["output_config"]["format"] == {"type": "json_schema", "schema": schemas.CLASSIFY_SCHEMA}
    assert result.usage.input_tokens == 1000
    assert result.usage.cost_usd == Decimal("0.004")  # 1000 * $2/M + 200 * $10/M


def test_retries_once_with_validation_error():
    bad = {**GOOD, "confidence": 7}
    client, calls = fake_anthropic(json.dumps(bad), json.dumps(GOOD))
    result = _call(AnthropicLLM(client))
    assert result.data.confidence == 0.9
    assert len(calls) == 2
    retry_messages = calls[1]["messages"]
    assert retry_messages[-1]["role"] == "user" and "failed validation" in retry_messages[-1]["content"]
    assert result.usage.calls == 2


def test_gives_up_after_second_invalid_output():
    client, calls = fake_anthropic("not json", json.dumps({"category": "nope"}))
    with pytest.raises(LLMValidationError) as exc:
        _call(AnthropicLLM(client))
    assert len(calls) == 2
    assert exc.value.usage.calls == 2


def test_refusal_raises():
    client, _ = fake_anthropic("", stop_reason="refusal")
    with pytest.raises(LLMError):
        _call(AnthropicLLM(client))


def test_effort_not_sent_to_haiku_4_5():
    client, calls = fake_anthropic(json.dumps(GOOD), json.dumps(GOOD))
    llm = AnthropicLLM(client)
    _call(llm, model="claude-haiku-4-5-20251001")
    _call(llm, model="claude-sonnet-5-5")
    assert "effort" not in calls[0]["output_config"]
    assert calls[1]["output_config"]["effort"] == "low"


def test_cost_uses_dated_model_prefix():
    assert cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == Decimal("1")
    assert cost_usd("unknown-model", 1000, 1000) == Decimal("0")
