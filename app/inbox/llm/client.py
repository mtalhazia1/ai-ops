"""Single entry point for every LLM call (Section 8).

- Structured output: the JSON schema goes in `output_config.format`, so the API
  guarantees schema-shaped JSON. (The brief planned forced tool use; current
  Sonnet/Opus models reject a forced `tool_choice`, structured outputs works on all.)
- The result is validated with pydantic. On a validation error we retry once with
  the error message, then raise `LLMValidationError` so the caller routes to review.
- Timeouts (30 s) and retries on 429/5xx/connection errors come from the SDK client.
- Every call returns token usage, cost and latency for the Triage / Draft rows.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from django.conf import settings
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """The call failed (API error, refusal, truncated output)."""

    def __init__(self, message: str, usage: "Usage | None" = None):
        super().__init__(message)
        self.usage = usage


class LLMValidationError(LLMError):
    """The output did not validate, even after one retry."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> "Usage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        self.latency_ms += other.latency_ms
        self.calls += other.calls
        return self


@dataclass
class StructuredResult:
    data: BaseModel
    usage: Usage = field(default_factory=Usage)


class LLM(Protocol):
    def structured(
        self, *, model: str, system: str, user: str, schema: dict[str, Any], validator: type[BaseModel], max_tokens: int = 2048
    ) -> StructuredResult: ...


def price_for(model: str) -> tuple[Decimal, Decimal]:
    prices = settings.MODEL_PRICES_PER_MTOK
    # Match "claude-haiku-4-5-20251001" to "claude-haiku-4-5": longest known prefix wins.
    for key in sorted(prices, key=len, reverse=True):
        if model == key or model.startswith(key + "-"):
            return prices[key]
    logger.warning("No price configured for model %s; cost recorded as 0", model)
    return Decimal("0"), Decimal("0")


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    price_in, price_out = price_for(model)
    return (price_in * input_tokens + price_out * output_tokens) / Decimal(1_000_000)


def _supports_effort(model: str) -> bool:
    # Haiku 4.5 rejects output_config.effort; every newer model accepts it.
    return not model.startswith("claude-haiku-4-5")


class AnthropicLLM:
    def __init__(self, client: Any | None = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY or None,
                timeout=settings.LLM_TIMEOUT_SECONDS,
                max_retries=settings.LLM_MAX_RETRIES,
            )
        self.client = client

    def _call(self, *, model: str, system: str, messages: list[dict[str, Any]], schema: dict[str, Any], max_tokens: int):
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if _supports_effort(model) and settings.LLM_EFFORT:
            output_config["effort"] = settings.LLM_EFFORT
        started = time.monotonic()
        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config=output_config,
            )
        except Exception as exc:  # anthropic.APIError and friends; already retried by the SDK
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=cost_usd(model, response.usage.input_tokens, response.usage.output_tokens),
            latency_ms=latency_ms,
            calls=1,
        )
        if response.stop_reason == "refusal":
            raise LLMError("model refused the request", usage)
        if response.stop_reason == "max_tokens":
            raise LLMError("output truncated at max_tokens", usage)
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        return text, usage

    def structured(self, *, model, system, user, schema, validator, max_tokens=2048):
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        total = Usage()
        last_error = ""
        for attempt in range(2):
            text, usage = self._call(model=model, system=system, messages=messages, schema=schema, max_tokens=max_tokens)
            total.add(usage)
            try:
                return StructuredResult(data=validator.model_validate(json.loads(text)), usage=total)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)
                logger.info("LLM output failed validation (attempt %s): %s", attempt + 1, last_error[:300])
                messages = messages + [
                    {"role": "assistant", "content": text or "(empty)"},
                    {
                        "role": "user",
                        "content": f"That output failed validation:\n{last_error[:2000]}\nReturn corrected JSON only.",
                    },
                ]
        raise LLMValidationError(last_error, total)


_default: LLM | None = None


def get_llm() -> LLM:
    global _default
    if _default is None:
        _default = AnthropicLLM()
    return _default


def set_llm(llm: LLM | None) -> None:
    """Swap the LLM (tests use a fake)."""
    global _default
    _default = llm
