"""One adapter for any OpenAI-compatible Chat Completions endpoint (Groq, Gemini, ...).

The only code that knows a vendor exists. Maps vendor errors onto the three neutral error types.
"""
from __future__ import annotations

import time

import openai
from openai import OpenAI

from app.llm.base import (LLMTurn, MalformedToolCall, Message, ProviderError, ProviderUnavailable, ToolCall,
                          ToolChoice)

MAX_INLINE_WAIT_S = 8.0   # wait this long on a 429 before giving up on this provider
_MALFORMED = ("tool call validation failed", "tool_use_failed", "failed to call a function",
              "tool choice is required", "did not call a tool", "parsing failed")


class OpenAICompatProvider:
    def __init__(self, name: str, base_url: str, api_key: str, model: str, max_output_tokens: int,
                 timeout_s: float = 60.0, sleep=time.sleep, extra: dict | None = None):
        self.name = name
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0)
        self.sleep = sleep
        self.extra = extra or {}   # vendor/model-specific request fields, e.g. {"reasoning_effort": "low"}

    def complete(self, messages: list[Message], tools: list[dict], tool_choice: ToolChoice) -> LLMTurn:
        for attempt in (1, 2):
            try:
                r = self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=tools, tool_choice=tool_choice,  # type: ignore[arg-type]
                    max_completion_tokens=self.max_output_tokens, **self.extra)
                break
            except openai.RateLimitError as e:
                wait = _retry_after(e)
                if attempt == 1 and wait is not None and wait <= MAX_INLINE_WAIT_S:
                    self.sleep(wait)
                    continue
                raise ProviderUnavailable(f"{self.name}: rate limited", retry_after=wait) from e
            except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as e:
                raise ProviderUnavailable(f"{self.name}: {type(e).__name__}") from e
            except openai.BadRequestError as e:
                msg = str(e).lower()
                if any(s in msg for s in _MALFORMED):
                    raise MalformedToolCall(f"{self.name}: {str(e)[:200]}") from e
                raise ProviderError(f"{self.name}: bad request: {str(e)[:200]}") from e
            except openai.APIStatusError as e:
                raise ProviderError(f"{self.name}: HTTP {e.status_code}") from e
        msg = r.choices[0].message
        calls = [ToolCall(c.id, c.function.name, c.function.arguments or "{}") for c in (msg.tool_calls or [])]
        usage = {}
        if r.usage:
            usage = {"prompt": r.usage.prompt_tokens, "completion": r.usage.completion_tokens,
                     "total": r.usage.total_tokens}
        return LLMTurn(msg.content, calls, r.choices[0].finish_reason, self.name, self.model, usage)


def _retry_after(e: openai.RateLimitError) -> float | None:
    try:
        v = e.response.headers.get("retry-after")
        return float(v) if v is not None else None
    except (ValueError, AttributeError):
        return None
