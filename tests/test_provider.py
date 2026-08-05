"""Provider tests using a fake OpenAI client — no network, no quota spend."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from askbharat.llm.limiter import DailyQuota
from askbharat.llm.provider import (
    EmptyCompletion,
    LLMProvider,
    SchemaValidationFailed,
)


class Tiny(BaseModel):
    name: str
    fee: float | None = None


def _resp(content, *, finish="stop", reasoning=""):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, reasoning=reasoning),
            finish_reason=finish,
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=FakeCompletions(script))


def make_provider(tmp_path, script, chains=None):
    p = LLMProvider(api_key="test", chains=chains or {"t": ["model-a", "model-b"]})
    p.quota = DailyQuota(cap=100, path=tmp_path / "q.json")
    p._client = FakeClient(script)
    return p


def test_plain_completion_returns_text(tmp_path):
    p = make_provider(tmp_path, [_resp("hello")])
    c = p.complete([{"role": "user", "content": "hi"}], task="t")
    assert c.text == "hello"
    assert c.usage.model == "model-a"
    assert not c.usage.fell_back


def test_empty_content_raises_budget_not_schema_error(tmp_path):
    """A reasoning model that spends its budget must trigger a budget bump.

    This is the gpt-oss failure mode: HTTP 200, finish_reason='length', empty
    content. Treating it as a schema failure wastes a request and fails the
    same way, so the provider must raise max_tokens and retry instead.
    """
    p = make_provider(tmp_path, [
        _resp("", finish="length", reasoning="thinking..."),
        _resp('{"name": "Passport", "fee": 1500}'),
    ])
    c = p.complete([{"role": "user", "content": "x"}], task="t",
                   schema=Tiny, max_tokens=16)
    assert c.parsed.name == "Passport"
    assert c.usage.budget_raised
    calls = p._client.chat.completions.calls
    assert calls[0]["max_tokens"] == 16
    assert calls[1]["max_tokens"] == 64, "budget should escalate 4x"


def test_budget_escalation_stops_at_ceiling(tmp_path):
    p = make_provider(tmp_path, [_resp("", finish="length")] * 40,
                      chains={"t": ["model-a"]})
    with pytest.raises((EmptyCompletion, SchemaValidationFailed)):
        p.complete([{"role": "user", "content": "x"}], task="t",
                   schema=Tiny, max_tokens=1000)


def test_schema_failure_retries_once_then_falls_back(tmp_path):
    p = make_provider(tmp_path, [
        _resp("not json"),                          # model-a attempt 1
        _resp("still not json"),                    # model-a attempt 2
        _resp('{"name": "Aadhaar", "fee": null}'),  # model-b succeeds
    ])
    c = p.complete([{"role": "user", "content": "x"}], task="t", schema=Tiny)
    assert c.parsed.name == "Aadhaar"
    assert c.parsed.fee is None
    assert c.usage.fell_back, "should have moved to the second model"


def test_json_fences_are_stripped(tmp_path):
    p = make_provider(tmp_path, [_resp('```json\n{"name": "PAN"}\n```')])
    c = p.complete([{"role": "user", "content": "x"}], task="t", schema=Tiny)
    assert c.parsed.name == "PAN"


def test_all_models_failing_raises(tmp_path):
    p = make_provider(tmp_path, [_resp("bad")] * 8)
    with pytest.raises(SchemaValidationFailed):
        p.complete([{"role": "user", "content": "x"}], task="t", schema=Tiny)


def test_quota_is_refunded_when_request_never_lands(tmp_path):
    from openai import APIConnectionError

    p = make_provider(tmp_path, [
        APIConnectionError(request=None),
        _resp('{"name": "OK"}'),
    ])
    p.complete([{"role": "user", "content": "x"}], task="t", schema=Tiny)
    # 2 reserved, 1 refunded for the connection error that never reached them.
    assert p.quota.used == 1


def test_no_choices_in_200_response_is_retried_not_crashed(tmp_path):
    """OpenRouter wraps some upstream failures in a 200 with choices=None.

    Indexing choices[0] blindly killed a live run. It must be treated as a
    retryable failure, not an exception.
    """
    broken = SimpleNamespace(choices=None, usage=None, error="upstream timeout")
    p = make_provider(tmp_path, [broken, _resp('{"name": "PAN"}')])
    c = p.complete([{"role": "user", "content": "x"}], task="t", schema=Tiny)
    assert c.parsed.name == "PAN"


def test_empty_choices_list_also_retried(tmp_path):
    empty = SimpleNamespace(choices=[], usage=None)
    p = make_provider(tmp_path, [empty, _resp('{"name": "Aadhaar"}')])
    c = p.complete([{"role": "user", "content": "x"}], task="t", schema=Tiny)
    assert c.parsed.name == "Aadhaar"
