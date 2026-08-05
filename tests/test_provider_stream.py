"""Streaming completions — fallback, quota, and the point of no return.

The rule that makes streaming different from `complete()`: a model can only be
swapped out before the first character reaches the reader. After that the text
is on the citizen's screen, and a second model would either restart it or
contradict it. These tests pin both sides of that line.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai import APIConnectionError

from askbharat.llm.limiter import DailyQuota, DailyQuotaExceeded
from askbharat.llm.provider import (
    LLMProvider,
    SchemaValidationFailed,
    StreamInterrupted,
)


def chunk(content=None, reasoning=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, reasoning=reasoning))])


def no_choices():
    """OpenRouter wraps some upstream errors in a 200 with no choices."""
    return SimpleNamespace(choices=[])


class Boom(APIConnectionError):
    def __init__(self):
        super().__init__(request=SimpleNamespace())


class FailMidStream:
    """Yields some content, then dies — the un-retryable case."""

    def __iter__(self):
        yield chunk("Yes, there are ")
        yield chunk("two schemes")
        raise Boom()


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


def make_provider(tmp_path, script, cap=100):
    p = LLMProvider(api_key="test", chains={"answer": ["model-a", "model-b"]})
    p.quota = DailyQuota(cap=cap, path=tmp_path / "q.json")
    p._client = SimpleNamespace(chat=SimpleNamespace(
        completions=FakeCompletions(script)))
    return p


def test_deltas_are_yielded_in_order(tmp_path):
    p = make_provider(tmp_path, [[chunk("Hello "), chunk("world")]])
    assert list(p.stream([{"role": "user", "content": "hi"}])) == [
        "Hello ", "world",
    ]


def test_stream_requests_streaming_from_the_api(tmp_path):
    p = make_provider(tmp_path, [[chunk("x")]])
    list(p.stream([{"role": "user", "content": "hi"}]))
    assert p._client.chat.completions.create.__self__.calls[0]["stream"] is True


def test_reasoning_deltas_are_not_shown(tmp_path):
    """Reasoning is not the answer; showing it puts noise where text belongs."""
    p = make_provider(tmp_path, [[
        chunk(reasoning="thinking about pensions"),
        chunk("The answer"),
    ]])
    assert list(p.stream([{"role": "user", "content": "hi"}])) == ["The answer"]


def test_empty_chunks_are_skipped(tmp_path):
    p = make_provider(tmp_path, [[no_choices(), chunk("ok"), no_choices()]])
    assert list(p.stream([{"role": "user", "content": "hi"}])) == ["ok"]


def test_failure_before_any_output_falls_back_to_next_model(tmp_path):
    p = make_provider(tmp_path, [Boom(), [chunk("from the fallback")]])
    out = list(p.stream([{"role": "user", "content": "hi"}]))
    assert out == ["from the fallback"]
    calls = p._client.chat.completions.create.__self__.calls
    assert [c["model"] for c in calls] == ["model-a", "model-b"]


def test_a_model_that_streams_nothing_falls_through(tmp_path):
    p = make_provider(tmp_path, [[], [chunk("second model answered")]])
    assert list(p.stream([{"role": "user", "content": "hi"}])) == [
        "second model answered",
    ]


def test_failure_after_output_raises_rather_than_restarting(tmp_path):
    """The point of no return: text is on screen, so no silent second attempt."""
    p = make_provider(tmp_path, [FailMidStream(), [chunk("SHOULD NOT APPEAR")]])
    seen = []
    with pytest.raises(StreamInterrupted):
        for piece in p.stream([{"role": "user", "content": "hi"}]):
            seen.append(piece)
    assert seen == ["Yes, there are ", "two schemes"]
    # The fallback model was never called.
    calls = p._client.chat.completions.create.__self__.calls
    assert [c["model"] for c in calls] == ["model-a"]


def test_all_models_failing_raises(tmp_path):
    p = make_provider(tmp_path, [Boom(), Boom()])
    with pytest.raises(SchemaValidationFailed):
        list(p.stream([{"role": "user", "content": "hi"}]))


def test_a_failed_request_is_refunded_but_a_used_one_is_not(tmp_path):
    """Quota is spent on answers delivered, not on connections attempted."""
    p = make_provider(tmp_path, [Boom(), [chunk("ok")]])
    list(p.stream([{"role": "user", "content": "hi"}]))
    assert p.quota.used == 1          # 2 consumed, the failed one refunded


def test_exhausted_quota_raises_before_calling_the_model(tmp_path):
    p = make_provider(tmp_path, [[chunk("never reached")]], cap=0)
    with pytest.raises(DailyQuotaExceeded):
        list(p.stream([{"role": "user", "content": "hi"}]))
    assert p._client.chat.completions.create.__self__.calls == []
