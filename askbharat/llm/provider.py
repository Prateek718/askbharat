"""The single LLM interface.

Every model call in the system goes through `complete()`. The model is chosen
per *task* from config, never named at the call site — so switching model or
provider is a config change, not a code change. That matters here because the
plan deliberately starts on free models whose quality is unproven for this
workload; the Phase 6 eval decides whether they stay, and the switch has to be
cheap either way.

OpenRouter speaks the OpenAI API, so this is the `openai` SDK with a different
base URL.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from openai import APIConnectionError, APIError, APIStatusError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from askbharat.config import redact, settings
from askbharat.llm.limiter import DailyQuota, DailyQuotaExceeded, TokenBucket

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"

# Verified live against OpenRouter's /models endpoint. All are $0/$0.
# Ordered as fallback chains: capable first, then progressively cheaper/smaller.
# Only models advertising structured-output support belong in an extraction chain.
MODEL_CHAINS: dict[str, list[str]] = {
    # Schema-constrained extraction. Needs structured outputs and a big context
    # window (government pages are verbose and we send them whole).
    # Ordered by measured throughput on real myScheme pages, not by parameter
    # count. Timed over one 11.9k-char scheme page:
    #   gpt-oss-20b   102s, valid record, 6/15 fields populated
    #   nemotron-120b never returned a record inside 15 minutes — it spends the
    #                 completion budget on reasoning, triggering budget
    #                 escalation and retry loops
    #   gemma-4-31b   provider-side 429 before it could answer
    # At ~100s/page the whole corpus is days of wall clock, so a model that is
    # marginally better per page but 10x slower is not a better model here.
    "extract": [
        "openai/gpt-oss-20b:free",                  # 131k ctx — fastest reliable
        "nvidia/nemotron-3-super-120b-a12b:free",   # 262k ctx — fallback
        "google/gemma-4-31b-it:free",               # 262k ctx — often throttled
    ],
    # Short, cheap, high-volume: query parsing, intent detection, alias expansion.
    "classify": [
        "openai/gpt-oss-20b:free",
        "google/gemma-4-31b-it:free",
    ],
    # Grounded answering, and the only chain the live site exercises.
    # gemma-4-31b leads because the deploy adds a Google AI Studio key to the
    # OpenRouter account (Settings -> Integrations), so `:free` gemma calls now
    # route through that key's own Google quota instead of OpenRouter's shared
    # free pool. The shared pool is chronically 429'd upstream
    # ("limit_source: upstream_provider_shared_pool") and was what left the
    # deployed assistant returning nothing. nemotron-120b stays as the fallback
    # for when the Google quota is spent; openrouter/free is the last resort.
    "answer": [
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter/free",                          # auto-router, last resort
    ],
}


# Generous against a slow free-tier model on a long page (the budget can
# escalate to 32k tokens), tight enough that a socket killed by a laptop
# suspend fails in minutes rather than never.
REQUEST_TIMEOUT_S = 240.0

# A schema failure gets one corrective retry; a budget bump doesn't count
# against this, so a reasoning model gets room to grow before we give up.
MAX_ATTEMPTS_PER_MODEL = 2
# Ceiling for automatic budget escalation. Above this, the model is failing for
# some other reason and more tokens won't help.
MAX_TOKEN_CEILING = 32_000


class SchemaValidationFailed(RuntimeError):
    """Model returned JSON that doesn't fit the schema, twice."""


class EmptyCompletion(RuntimeError):
    """Model returned no content — usually the reasoning budget ate the ceiling."""


@dataclass
class Usage:
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Several free models (the gpt-oss and nemotron reasoning variants) spend
    # part of the completion budget on a `reasoning` field before emitting any
    # content. Tracked separately because it is the difference between "the
    # model failed" and "we didn't give it room to answer".
    reasoning_chars: int = 0
    attempts: int = 0
    fell_back: bool = False
    budget_raised: bool = False
    waited_s: float = 0.0


@dataclass
class Completion:
    text: str
    parsed: Any | None
    usage: Usage
    raw: Any = field(default=None, repr=False)


class LLMProvider:
    def __init__(
        self,
        api_key: str | None = None,
        daily_cap: int = 1000,
        rate_per_min: int = 20,
        chains: dict[str, list[str]] | None = None,
    ):
        self._api_key = api_key or settings.openrouter_api_key
        self.chains = chains or MODEL_CHAINS
        self.bucket = TokenBucket(rate_per_min=rate_per_min)
        self.quota = DailyQuota(cap=daily_cap)
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            if not self._api_key:
                raise RuntimeError(
                    "OPENROUTER_API_KEY is not set. Copy .env.example to .env "
                    "and add a key from https://openrouter.ai/keys"
                )
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=BASE_URL,
                # Explicit, and shorter than the SDK's 600s default. This box
                # suspends nightly, and a suspend silently kills every open
                # socket: on resume the worker sat at 0% CPU with three tasks
                # claimed 9.2 hours earlier, blocked on reads that would never
                # return. A bounded timeout turns that permanent hang into a
                # retryable error the existing fallback path already handles.
                timeout=REQUEST_TIMEOUT_S,
                # Retries here are transport-level and separate from the model
                # chain; keep them low so a dead endpoint falls through to the
                # next model quickly rather than multiplying the timeout.
                max_retries=1,
                default_headers={
                    # OpenRouter uses these for attribution on free models.
                    "HTTP-Referer": settings.crawler_contact,
                    "X-Title": "askbharat",
                },
            )
        return self._client

    def models_for(self, task: str) -> list[str]:
        if task not in self.chains:
            raise KeyError(f"unknown task {task!r}; known: {sorted(self.chains)}")
        return self.chains[task]

    def complete(
        self,
        messages: list[dict],
        *,
        task: str = "classify",
        schema: type[BaseModel] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> Completion:
        """Run a completion, falling back through the task's model chain.

        With `schema`, the response is constrained to that JSON schema and
        validated. A validation failure is retried *once on the same model* with
        the error fed back, then falls through to the next model. Free models
        are less reliable at schema adherence than frontier models, so the
        validator is the safety net — not the prompt.
        """
        usage = Usage(model="", attempts=0)
        last_error: Exception | None = None
        budget = max_tokens

        for model_idx, model in enumerate(self.models_for(task)):
            usage.fell_back = model_idx > 0
            attempt = 0
            while attempt < MAX_ATTEMPTS_PER_MODEL:
                attempt += 1
                usage.attempts += 1
                usage.waited_s += self.bucket.take()
                self.quota.consume(1)          # raises DailyQuotaExceeded -> park

                try:
                    kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "max_tokens": budget,
                        "temperature": temperature,
                    }
                    if schema is not None:
                        kwargs["response_format"] = {
                            "type": "json_schema",
                            "json_schema": {
                                "name": schema.__name__,
                                "strict": True,
                                "schema": schema.model_json_schema(),
                            },
                        }
                    resp = self.client.chat.completions.create(**kwargs)

                except RateLimitError as e:
                    # 429 from the provider — our bucket was too optimistic.
                    self.quota.refund(1)
                    last_error = e
                    time.sleep(min(2**attempt, 30))
                    continue
                except (APIStatusError, APIConnectionError, APIError) as e:
                    self.quota.refund(1)
                    last_error = e
                    log.warning("model %s failed: %s", model, redact(str(e))[:160])
                    break                       # try the next model
                except DailyQuotaExceeded:
                    raise

                # OpenRouter can return HTTP 200 with no choices at all when an
                # upstream free-tier provider fails — the error is wrapped in a
                # success envelope. Indexing straight into choices[0] crashes
                # the whole run on a transient upstream blip, so treat it as a
                # retryable failure like any other.
                if not getattr(resp, "choices", None):
                    err = getattr(resp, "error", None) or "no choices in response"
                    last_error = EmptyCompletion(f"{model}: {str(err)[:200]}")
                    log.warning("model %s returned no choices: %s", model,
                                str(err)[:160])
                    continue

                choice = resp.choices[0]
                text = (choice.message.content or "").strip()
                usage.model = model
                if resp.usage:
                    usage.prompt_tokens = resp.usage.prompt_tokens or 0
                    usage.completion_tokens = resp.usage.completion_tokens or 0
                usage.reasoning_chars = len(getattr(choice.message, "reasoning", "") or "")

                # Reasoning models spend the completion budget on `reasoning`
                # before writing any content. If the budget ran out first we get
                # HTTP 200, finish_reason="length", and an *empty* content field.
                # That is a budget problem, not a schema problem: feeding back a
                # validation error would waste another request and fail the same
                # way. Raise the ceiling and retry instead.
                if not text:
                    reason = choice.finish_reason
                    if budget < MAX_TOKEN_CEILING:
                        budget = min(budget * 4, MAX_TOKEN_CEILING)
                        usage.budget_raised = True
                        log.warning(
                            "%s returned empty content (finish_reason=%s, "
                            "reasoning=%d chars); raising max_tokens to %d",
                            model, reason, usage.reasoning_chars, budget,
                        )
                        attempt -= 1        # a budget bump is not a real attempt
                        continue
                    last_error = EmptyCompletion(
                        f"{model} produced no content within {budget} tokens "
                        f"(finish_reason={reason})"
                    )
                    break                   # give up on this model, try the next

                if schema is None:
                    return Completion(text=text, parsed=None, usage=usage, raw=resp)

                try:
                    parsed = schema.model_validate_json(_strip_fences(text))
                    return Completion(text=text, parsed=parsed, usage=usage, raw=resp)
                except (ValidationError, json.JSONDecodeError) as e:
                    last_error = e
                    log.warning(
                        "schema validation failed on %s (attempt %d): %s",
                        model, attempt, str(e)[:200],
                    )
                    if attempt == 1:
                        # Feed the error back once — cheap and often sufficient.
                        messages = messages + [
                            {"role": "assistant", "content": text[:2000]},
                            {
                                "role": "user",
                                "content": (
                                    "That response did not validate against the "
                                    f"schema. Error:\n{str(e)[:800]}\n\n"
                                    "Return only valid JSON matching the schema. "
                                    "Use null for anything the source does not state."
                                ),
                            },
                        ]

        raise SchemaValidationFailed(
            f"all models in chain {task!r} failed; last error: "
            f"{redact(str(last_error))[:300]}"
        )

    def stream(
        self,
        messages: list[dict],
        *,
        task: str = "answer",
        max_tokens: int = 1200,
        temperature: float = 0.0,
    ) -> Iterator[str]:
        """Yield content deltas as the model produces them.

        Streaming is for *answering*, not extraction: there is no schema to
        validate, so there is nothing to retry against, and the only reason to
        stream is that a citizen is waiting at the other end.

        Fallback works differently here than in `complete()`. A model can only
        be swapped out **before the first character reaches the reader** —
        after that the text is on screen and a second model would restart or
        contradict it. So a failure before the first delta falls through to the
        next model; a failure after it raises `StreamInterrupted`.

        Reasoning models emit a `reasoning` delta before any content. Those are
        skipped rather than shown: it is not the answer, and for the citizen it
        is noise arriving where the answer should be.
        """
        last_error: Exception | None = None

        for model in self.models_for(task):
            self.bucket.take()
            self.quota.consume(1)          # raises DailyQuotaExceeded -> caller parks
            emitted = False

            try:
                stream = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=True,
                )
                for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        # OpenRouter wraps some upstream errors in a 200 with
                        # no choices; mid-stream this is simply a dead chunk.
                        continue
                    delta = getattr(chunk.choices[0], "delta", None)
                    piece = getattr(delta, "content", None) if delta else None
                    if piece:
                        emitted = True
                        yield piece

                if emitted:
                    return
                # Ran to completion having produced only reasoning, or nothing
                # at all. Nothing has been shown, so the next model is free.
                last_error = EmptyCompletion(f"{model} streamed no content")
                log.warning("model %s streamed no content", model)

            except DailyQuotaExceeded:
                raise
            except (APIStatusError, APIConnectionError, APIError,
                    RateLimitError) as e:
                last_error = e
                log.warning("stream failed on %s: %s", model,
                            redact(str(e))[:160])
                if emitted:
                    raise StreamInterrupted(
                        f"{model} stopped mid-answer: {redact(str(e))[:200]}"
                    ) from e
                # Nothing reached the reader, so this request bought nothing.
                self.quota.refund(1)
                continue

        raise SchemaValidationFailed(
            f"all models in chain {task!r} failed to stream; last error: "
            f"{redact(str(last_error))[:300]}"
        )


class StreamInterrupted(RuntimeError):
    """The stream died after text had already reached the reader.

    Distinct from every other failure because it cannot be retried silently:
    the citizen has already seen a partial sentence on screen, so restarting on
    the next model would either duplicate it or contradict it. The caller must
    surface the break rather than paper over it.
    """


def _strip_fences(text: str) -> str:
    """Recover the JSON object from a response that is not purely JSON.

    Despite `json_schema` mode, free providers return three shapes we have to
    survive, and each one otherwise costs a corrective request:

      1. bare JSON                      -> returned as-is
      2. ```json ... ``` fenced         -> fence removed
      3. prose, then the object         -> e.g. an '**Extracted Fields**'
                                           heading before the '{'

    For (3) we take the first balanced brace span rather than a greedy
    first-'{'-to-last-'}' slice, so trailing commentary after the object does
    not get swallowed into it.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    if t.startswith("{"):
        return t

    start = t.find("{")
    if start == -1:
        return t
    depth, in_str, escaped = 0, False, False
    for i, ch in enumerate(t[start:], start):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return t


_default: LLMProvider | None = None


def provider() -> LLMProvider:
    global _default
    if _default is None:
        _default = LLMProvider()
    return _default
