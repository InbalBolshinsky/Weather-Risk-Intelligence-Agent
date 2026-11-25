"""Provider-neutral types. The neutral message format is the OpenAI Chat Completions message dict
({"role", "content", "tool_calls", "tool_call_id"}), which every candidate vendor accepts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Message = dict[str, Any]
ToolChoice = Literal["auto", "required"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str          # raw JSON text, validated by the caller


@dataclass
class LLMTurn:
    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class ProviderUnavailable(Exception):
    """Rate limited, overloaded, timed out, unreachable: try the next provider."""


class MalformedToolCall(Exception):
    """The model produced an invalid tool call (e.g. tool name 'json'): retry once, then the next provider."""


class ProviderError(Exception):
    """Any other provider failure (auth, bad request): don't retry this provider; try the next one."""


class LLMProvider(Protocol):
    name: str

    def complete(self, messages: list[Message], tools: list[dict], tool_choice: ToolChoice) -> LLMTurn: ...
