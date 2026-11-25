"""The agent loop (PLAN.md §2.6, §5.2, D12, D23, D27, D28).

One turn: [system + history + user] -> model calls tools (tool_choice="required") -> we run them ->
model calls submit_answer -> we validate (schema, semantics, grounding) -> one repair retry if needed ->
server adds confidence level + tables -> persist the turn.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.agent.prompts import system_prompt
from app.agent.schemas import SUBMIT_ANSWER_SPEC, AgentAnswer
from app.agent.tools import DATA_TOOLS, TOOL_SPECS, ToolContext, run_tool
from app.config import hub_registry
from app.llm.base import LLMProvider, Message
from app.llm.fallback import AllProvidersFailed

TOOLS = TOOL_SPECS + [SUBMIT_ANSWER_SPEC]
MAX_ROUNDS = 8
SMALL_INTS_OK = {str(i) for i in range(11)}   # list counters like "3 hubs" can't mislead; logged decision
_LEVELS = {"high": 2, "medium": 1, "low": 0}
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _norm_num(s: str) -> str:
    s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return s
    return str(int(v)) if v == int(v) else f"{v:g}"


def numbers_in(text: str) -> set[str]:
    return {_norm_num(m) for m in _NUM.findall(text or "")}


def ungrounded_numbers(answer_text: str, sources: list[str]) -> list[str]:
    allowed = set(SMALL_INTS_OK)
    for s in sources:
        allowed |= numbers_in(s)
    return sorted({n for n in numbers_in(answer_text) if n not in allowed})


@dataclass
class Verdict:
    answer: AgentAnswer | None
    problems: list[str] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.answer is not None and not self.problems

    @property
    def only_grounding(self) -> bool:
        return self.answer is not None and all(p.startswith("ungrounded") for p in self.problems)


@dataclass
class TurnResult:
    session_id: str
    answer: dict[str, Any]
    confidence: str | None
    tables: list[dict[str, Any]]
    unverified: bool
    ungrounded_numbers: list[str]
    debug: dict[str, Any]


def _error_answer(text: str) -> AgentAnswer:
    return AgentAnswer(response_type="error", intent="other", answer_markdown=text, locations=[],
                       clarifying_question=None, clarification_options=[], assumptions=[],
                       confidence_reason="", suggested_followups=[])


def _compact(answer: dict[str, Any]) -> str:
    places = ", ".join(loc.get("hub_id") or loc.get("label") or loc["query"] for loc in answer.get("locations", []))
    return answer.get("answer_markdown", "") + (f"\n[places: {places}]" if places else "")


class Agent:
    def __init__(self, provider: LLMProvider, ctx: ToolContext, today: date | None = None, max_rounds: int = MAX_ROUNDS):
        self.provider = provider
        self.ctx = ctx
        self.conn = ctx.http.conn
        self.today = today or ctx.today
        self.max_rounds = max_rounds

    # ------------------------------------------------------------------ history (D27)
    def _history(self, session_id: str) -> list[Message]:
        rows = self.conn.execute("SELECT user_message, full_json, answer_json FROM turns WHERE session_id=? "
                                 "ORDER BY seq", (session_id,)).fetchall()
        out: list[Message] = []
        for i, r in enumerate(rows):
            answer = json.loads(r["answer_json"])
            if i == len(rows) - 1:
                out.extend(json.loads(r["full_json"]))           # latest turn in full (tool results included)
            else:
                out.append({"role": "user", "content": r["user_message"]})   # older turns: question + answer only
            out.append({"role": "assistant", "content": _compact(answer)})
        return out

    def _save(self, session_id: str, user_message: str, full: list[Message], answer: dict[str, Any]) -> None:
        seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE session_id=?", (session_id,)).fetchone()[0]
        self.conn.execute("INSERT INTO turns(session_id, seq, user_message, full_json, answer_json, created_at) "
                          "VALUES (?,?,?,?,?,?)", (session_id, seq, user_message, json.dumps(full), json.dumps(answer),
                                                   datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    # ------------------------------------------------------------------ validation (§5.2)
    def _check(self, arguments: str, sources: list[str]) -> Verdict:
        try:
            ans = AgentAnswer.model_validate_json(arguments)
        except ValidationError as e:
            return Verdict(None, [f"schema: {str(e)[:400]}"])
        problems = []
        if ans.response_type == "clarification" and not (ans.clarifying_question or "").strip():
            problems.append("response_type is clarification but clarifying_question is empty")
        reg = hub_registry()
        bad = [loc.hub_id for loc in ans.locations if loc.hub_id and reg.get(loc.hub_id) is None]
        if bad:
            problems.append(f"unknown hub ids in locations: {bad}")
        ung = ungrounded_numbers(ans.answer_markdown, sources)
        if ung:
            problems.append(f"ungrounded numbers {ung}: every number must be quoted exactly from a tool result "
                            "(or an earlier answer); remove or replace these")
        return Verdict(ans, problems, ung)

    # ------------------------------------------------------------------ one turn
    def chat(self, message: str, session_id: str | None = None) -> TurnResult:
        t0 = time.perf_counter()
        session_id = session_id or uuid.uuid4().hex[:12]
        history = self._history(session_id)
        sys_prompt = system_prompt(self.today)
        user_msg: Message = {"role": "user", "content": message}
        messages: list[Message] = [{"role": "system", "content": sys_prompt}, *history, user_msg]
        turn_msgs: list[Message] = [user_msg]
        sources = [sys_prompt, message] + [m["content"] for m in history if isinstance(m.get("content"), str)]
        data_results: list[tuple[str, dict]] = []
        debug: dict[str, Any] = {"rounds": 0, "tool_calls": [], "providers": [], "tokens": 0, "events": []}
        events_before = len(getattr(self.provider, "events", []))
        repairs_left = 1
        final: AgentAnswer | None = None
        ungrounded: list[str] = []

        try:
            for _ in range(self.max_rounds):
                lt = self.provider.complete(messages, TOOLS, "required")
                debug["rounds"] += 1
                debug["tokens"] += lt.usage.get("total", 0)
                debug["providers"].append(f"{lt.provider}:{lt.model}")
                if not lt.tool_calls:   # shouldn't happen with tool_choice="required"; nudge once per round
                    messages.append({"role": "assistant", "content": lt.text or ""})
                    messages.append({"role": "user", "content": "Finish by calling submit_answer."})
                    continue
                assistant: Message = {"role": "assistant", "content": lt.text or "", "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in lt.tool_calls]}
                messages.append(assistant)
                turn_msgs.append(assistant)
                submit = None
                for call in lt.tool_calls:
                    if call.name == "submit_answer":
                        submit = submit or call
                        continue
                    c0 = time.perf_counter()
                    result, is_err = run_tool(self.ctx, call.name, call.arguments)
                    content = json.dumps(result, separators=(",", ":"))
                    debug["tool_calls"].append({"name": call.name, "arguments": call.arguments,
                                                "ms": int((time.perf_counter() - c0) * 1000), "error": is_err})
                    if not is_err and call.name in DATA_TOOLS:
                        data_results.append((call.name, result))
                        sources.append(content)
                    tool_msg = {"role": "tool", "tool_call_id": call.id, "content": content}
                    messages.append(tool_msg)
                    turn_msgs.append(tool_msg)
                # Every tool call needs a response; extra submit_answer calls get a short one.
                for call in lt.tool_calls:
                    if call.name == "submit_answer" and call is not submit:
                        dup = {"role": "tool", "tool_call_id": call.id, "content": '{"error":"duplicate submit_answer"}'}
                        messages.append(dup)
                        turn_msgs.append(dup)
                if submit is None:
                    continue
                verdict = self._check(submit.arguments, sources)
                accept = verdict.ok or (verdict.only_grounding and repairs_left == 0)
                reply = {"status": "accepted"} if accept else {
                    "status": "rejected", "problems": verdict.problems, "fix": "correct these and call submit_answer again"}
                submit_reply = {"role": "tool", "tool_call_id": submit.id, "content": json.dumps(reply)}
                messages.append(submit_reply)
                turn_msgs.append(submit_reply)
                if accept:
                    final, ungrounded = verdict.answer, verdict.ungrounded
                    break
                if repairs_left == 0:   # schema/semantic failure after the one repair: give up cleanly
                    final = _error_answer("Sorry, I couldn't produce a valid answer. Please rephrase or try again.")
                    debug["events"].append(f"answer rejected after repair: {verdict.problems}")
                    break
                repairs_left -= 1
                debug["events"].append(f"repair requested: {verdict.problems}")
            if final is None:
                final = _error_answer("Sorry, I couldn't finish this request within the step limit. Please narrow it down.")
        except AllProvidersFailed as e:
            final = _error_answer("The language model service is busy or unavailable right now (free-tier limits). "
                                  "Please try again in a minute.")
            debug["events"].append(f"all providers failed: {e}")
            turn_msgs = [user_msg]   # don't persist a half-finished tool exchange

        debug["events"] = getattr(self.provider, "events", [])[events_before:] + debug["events"]
        debug["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        answer = final.model_dump()

        # Server-side additions (D28): confidence level from the cited tool results; tables from this turn's data.
        levels = [r.get("confidence") for _, r in data_results if r.get("confidence")]
        confidence = (min(levels, key=lambda c: _LEVELS[c]) if levels and final.response_type == "answer" else None)
        latest: dict[str, dict] = {}
        for name, result in data_results:
            if name != "get_methodology":
                latest[name] = result
        tables = [{"tool": n, "data": r} for n, r in latest.items()] if final.response_type in ("answer", "clarification") else []

        self._save(session_id, message, turn_msgs, answer)
        return TurnResult(session_id, answer, confidence, tables, bool(ungrounded), ungrounded, debug)
