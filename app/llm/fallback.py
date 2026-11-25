"""Fallback chain over providers (D13): next provider on availability errors; one same-provider retry
on a malformed tool call. Answer-validation failures are NOT handled here (that's the agent's repair step)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from app.config import CONFIG_DIR
from app.llm.base import (LLMProvider, LLMTurn, MalformedToolCall, Message, ProviderError, ProviderUnavailable,
                          ToolChoice)
from app.llm.openai_compat import OpenAICompatProvider


class AllProvidersFailed(Exception):
    pass


class FallbackProvider:
    def __init__(self, providers: list[LLMProvider]):
        if not providers:
            raise AllProvidersFailed("no LLM provider configured (set GROQ_API_KEY in .env)")
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.events: list[str] = []   # per-call log of fallbacks/retries, surfaced in the API debug field

    def complete(self, messages: list[Message], tools: list[dict], tool_choice: ToolChoice) -> LLMTurn:
        errors = []
        for p in self.providers:
            for attempt in (1, 2):
                try:
                    return p.complete(messages, tools, tool_choice)
                except MalformedToolCall as e:
                    self.events.append(f"malformed tool call from {p.name} (attempt {attempt})")
                    errors.append(str(e))
                    if attempt == 1:
                        continue
                except (ProviderUnavailable, ProviderError) as e:
                    self.events.append(f"{type(e).__name__}: {e}")
                    errors.append(str(e))
                break
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
                                              entry["max_output_tokens"], cfg.get("timeout_s", 60)))
    return FallbackProvider(providers)
