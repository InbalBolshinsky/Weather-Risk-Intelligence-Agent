"""Fallback chain over providers (D13): next provider on availability errors; one same-provider retry
on a malformed tool call. Answer-validation failures are NOT handled here (that's the agent's repair step)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

from app.config import CONFIG_DIR
from app.llm.base import (LLMProvider, LLMTurn, MalformedToolCall, Message, ProviderError, ProviderUnavailable,
                          ToolChoice)
from app.llm.openai_compat import OpenAICompatProvider


class AllProvidersFailed(Exception):
    """`retry_after` (seconds) is set when every provider was rate-limited: the shortest wait any of them reported.
    `malformed_only` is True when every failure was a malformed tool call (no capacity problem at all, D80)."""

    def __init__(self, message: str, retry_after: float | None = None, malformed_only: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.malformed_only = malformed_only


def _shortest_wait(waits: list[float | None]) -> float | None:
    return min(waits) if waits and all(w is not None for w in waits) else None  # type: ignore[type-var]


def _all_malformed(kinds: list[str]) -> bool:
    return bool(kinds) and all(k == "malformed" for k in kinds)


MAX_CAPACITY_WAIT_S = 30.0   # D40: when every provider is rate-limited, wait once (at most this long), then retry


class FallbackProvider:
    def __init__(self, providers: list[LLMProvider], sleep=time.sleep):
        if not providers:
            raise AllProvidersFailed("no LLM provider configured (set GROQ_API_KEY in .env)")
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.sleep = sleep
        self.events: list[str] = []   # per-call log of fallbacks/retries, surfaced in the API debug field

    def _pass(self, messages: list[Message], tools: list[dict], tool_choice: ToolChoice,
              errors: list[str], waits: list[float | None], kinds: list[str]) -> LLMTurn | None:
        for p in self.providers:
            for attempt in (1, 2):
                try:
                    return p.complete(messages, tools, tool_choice)
                except MalformedToolCall as e:
                    self.events.append(f"malformed tool call from {p.name} (attempt {attempt})")
                    errors.append(str(e))
                    waits.append(None)
                    kinds.append("malformed")
                    if attempt == 1:
                        continue
                except ProviderUnavailable as e:
                    self.events.append(f"ProviderUnavailable: {e}")
                    errors.append(str(e))
                    waits.append(e.retry_after)
                    kinds.append("unavailable")
                except ProviderError as e:
                    self.events.append(f"ProviderError: {e}")
                    errors.append(str(e))
                    waits.append(None)
                    kinds.append("error")
                break
        return None

    def complete(self, messages: list[Message], tools: list[dict], tool_choice: ToolChoice) -> LLMTurn:
        errors: list[str] = []
        waits: list[float | None] = []
        kinds: list[str] = []
        turn = self._pass(messages, tools, tool_choice, errors, waits, kinds)
        if turn is not None:
            return turn
        # D40: if every provider failed only because of rate limits, wait once for the shortest reported
        # retry time, then try the whole chain again. D74: if that wait is longer than the cap (e.g. the daily
        # token limit), waiting is pointless: fail now and pass the real wait on, so the user can be told.
        shortest = _shortest_wait(waits)
        if shortest is not None:
            if shortest > MAX_CAPACITY_WAIT_S:
                self.events.append(f"all providers rate-limited for {shortest:.0f}s or more: not waiting")
                raise AllProvidersFailed("; ".join(errors), retry_after=shortest)
            wait = max(shortest, 1.0)
            self.events.append(f"all providers rate-limited: waited {wait:.0f}s for capacity")
            self.sleep(wait)
            waits, kinds = [], []
            turn = self._pass(messages, tools, tool_choice, errors, waits, kinds)
            if turn is not None:
                return turn
            raise AllProvidersFailed("; ".join(errors), retry_after=_shortest_wait(waits),
                                     malformed_only=_all_malformed(kinds))
        raise AllProvidersFailed("; ".join(errors), malformed_only=_all_malformed(kinds))


def build_chain(path: Path | None = None, only: str | None = None) -> FallbackProvider:
    """Build the chain from config/llm.yaml + environment. `only` restricts it to one named entry."""
    cfg = yaml.safe_load((path or CONFIG_DIR / "llm.yaml").read_text())
    providers: list[LLMProvider] = []
    for entry in cfg["chain"]:
        if only and entry["name"] != only:
            continue
        if not only and entry.get("enabled", True) is False:
            continue
        key = os.getenv(entry["key_env"])
        if not key:
            continue
        model = os.getenv(entry.get("model_env", ""), "") or entry["model"]
        providers.append(OpenAICompatProvider(entry["name"], entry["base_url"], key, model,
                                              entry["max_output_tokens"], cfg.get("timeout_s", 60),
                                              extra=entry.get("extra")))
    return FallbackProvider(providers)
