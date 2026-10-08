import json
from types import SimpleNamespace

import pytest
from django.test import Client

from inbox.llm import client as llm_client
from inbox.llm.client import StructuredResult, Usage

TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.INTERNAL_TOKEN = TOKEN
    settings.ANTHROPIC_API_KEY = "test"
    yield
    llm_client.set_llm(None)


@pytest.fixture
def api(db):
    c = Client()

    def call(method, path, payload=None, token=TOKEN):
        headers = {"HTTP_X_INTERNAL_TOKEN": token} if token else {}
        fn = getattr(c, method)
        if payload is None:
            return fn(f"/internal{path}", **headers)
        return fn(f"/internal{path}", data=json.dumps(payload), content_type="application/json", **headers)

    return call


class FakeLLM:
    """Returns canned classification / extraction outputs, keyed by schema kind."""

    def __init__(self, classification=None, fields=None, evidence=None, confidence=None):
        self.classification = classification or {}
        self.fields = fields or {}
        self.evidence = evidence or {}
        self.confidence = confidence or {}
        self.calls = []

    def structured(self, *, model, system, user, schema, validator, max_tokens=2048):
        self.calls.append({"model": model, "system": system, "user": user})
        if "category" in schema["properties"]:
            data = {
                "category": "quote_request",
                "urgency": "normal",
                "confidence": 0.95,
                "reason": "asks for a rate",
                "contains_instructions_to_ai": False,
                **self.classification,
            }
        else:
            items = []
            for name, value in self.fields.items():
                for v in value if isinstance(value, list) else [value]:
                    if v is None:
                        continue
                    text = str(v).lower() if isinstance(v, bool) else str(v)
                    items.append({"field": name, "value": text, "evidence": self.evidence.get(name) or text,
                                  "confidence": self.confidence.get(name, 0.9)})
            data = {"items": items}
        return StructuredResult(data=validator.model_validate(data), usage=Usage(input_tokens=100, output_tokens=20, calls=1))


@pytest.fixture
def fake_llm():
    def make(**kwargs):
        llm = FakeLLM(**kwargs)
        llm_client.set_llm(llm)
        return llm

    return make


def fake_anthropic(*texts, stop_reason="end_turn"):
    """A stand-in for anthropic.Anthropic() returning the given texts in order."""
    calls = []
    queue = list(texts)

    def create(**kwargs):
        calls.append(kwargs)
        text = queue.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
            stop_reason=stop_reason,
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls
