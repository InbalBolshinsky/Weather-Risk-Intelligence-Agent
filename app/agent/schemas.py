"""The LLM <-> code contract: what the model submits via `submit_answer` (PLAN.md §5, D12, D28)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

ResponseType = Literal["answer", "clarification", "out_of_scope", "error"]
Intent = Literal["rank", "compare", "explain", "weather_stat", "current_alerts", "methodology", "other"]
PlaceStatus = Literal["hub_exact", "hub_alias", "hub_fuzzy", "geocoded", "llm_interpreted",
                      "ambiguous", "not_found", "non_us"]


class LocationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    hub_id: str | None
    label: str | None
    status: PlaceStatus


class AgentAnswer(BaseModel):
    """Submitted by the model. Confidence level and tables are added by the server, never by the model."""
    model_config = ConfigDict(extra="forbid")
    response_type: ResponseType
    intent: Intent
    answer_markdown: str
    locations: list[LocationOut]
    clarifying_question: str | None
    clarification_options: list[str]
    assumptions: list[str]
    confidence_reason: str
    suggested_followups: list[str]


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema -> the strict subset both vendors enforce:
    $defs inlined, every property required (optional = nullable), additionalProperties false, no titles."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].split("/")[-1]])
            node = {k: fix(v) for k, v in node.items() if k not in ("title", "default", "description")}
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            return node
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(schema)


SUBMIT_ANSWER_SPEC = {
    "type": "function",
    "function": {
        "name": "submit_answer",
        "description": "Submit the final answer to the user. Call exactly once, as the last step.",
        "strict": True,
        "parameters": strict_schema(AgentAnswer),
    },
}
