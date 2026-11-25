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
    pass


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
              errors: list[str], waits: list[float | None]) -> LLMTurn | None:
        for p in self.providers:
            for attempt in (1, 2):
                try:
                    return p.complete(messages, tools, tool_choice)
                except MalformedToolCall as e:
                    self.events.append(f"malformed tool call from {p.name} (attempt {attempt})")
                    errors.append(str(e))
                    waits.append(None)
                    if attempt == 1:
                        continue
                except ProviderUnavailable as e:
                    self.events.append(f"ProviderUnavailable: {e}")
                    errors.append(str(e))
                    waits.append(e.retry_after)
                except ProviderError as e:
                    self.events.append(f"ProviderError: {e}")
                    errors.append(str(e))
                    waits.append(None)
                break
        return None

    def complete(self, messages: list[Message], tools: list[dict], tool_choice: ToolChoice) -> LLMTurn:
        errors: list[str] = []
        waits: list[float | None] = []
        turn = self._pass(messages, tools, tool_choice, errors, waits)
        if turn is not None:
            return turn
        # D40: if every provider failed only because of rate limits, wait once for the shortest reported
        # retry time (capped), then try the whole chain again.
        if waits and all(w is not None for w in waits):
            wait = min(max(min(waits), 1.0), MAX_CAPACITY_WAIT_S)  # type: ignore[type-var]
            self.events.append(f"all providers rate-limited: waited {wait:.0f}s for capacity")
            self.sleep(wait)
            turn = self._pass(messages, tools, tool_choice, errors, [])
            if turn is not None:
                return turn
        raise AllProvidersFailed("; ".join(errors))


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
