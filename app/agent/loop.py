"""The agent loop (PLAN.md §2.6, §5.2; D12, D23, D27, D41, D43, D44, D50).

One turn: [system + history + user] -> model calls tools (tool_choice="required") -> we run them ->
model calls submit_answer -> we validate (schema, semantics, grounding, confidence line) -> one repair
retry if needed -> persist the turn. The user sees only answer_markdown (D41); raw tool results go to
`debug` for evals and inspection.
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
_LEVELS = {"high": 2, "medium": 1, "low": 0}
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_CONF = re.compile(r"confidence\**\s*[:\-–—]\s*\**\s*(high|medium|low)\b", re.IGNORECASE)   # tolerates **bold**


def _norm_num(s: str) -> str:
    s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return s
    return str(int(v)) if v == int(v) else f"{v:g}"


def numbers_in(text: str) -> set[str]:
    return {_norm_num(m) for m in _NUM.findall(text or "")}


def ungrounded_numbers(answer_text: str, sources: list[str], extra_allowed: set[str] | frozenset = frozenset()) -> list[str]:
    """D44: every number must appear in a tool result, the conversation, or the prompt (no exemptions)."""
    allowed: set[str] = set(extra_allowed)
    for s in sources:
        allowed |= numbers_in(s)
    return sorted({n for n in numbers_in(answer_text) if n not in allowed}, key=lambda x: float(x))


def stated_confidence(text: str) -> str | None:
    found = _CONF.findall(text or "")
    return found[-1].lower() if found else None


@dataclass
class Verdict:
    answer: AgentAnswer | None
    problems: list[str] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)
    confidence_wrong: bool = False

    @property
    def ok(self) -> bool:
        return self.answer is not None and not self.problems

    @property
    def fixable_by_code(self) -> bool:
        """Only grounding/confidence problems remain: the answer is usable and code can annotate it (D50)."""
        return self.answer is not None and all(p.startswith(("ungrounded", "confidence")) for p in self.problems)


@dataclass
class TurnResult:
    session_id: str
    answer: dict[str, Any]
    confidence: str | None
    unverified: bool
    ungrounded_numbers: list[str]
    debug: dict[str, Any]


def _error_answer(text: str) -> AgentAnswer:
    return AgentAnswer(response_type="error", intent="other", answer_markdown=text, locations=[],
                       clarifying_question=None, clarification_options=[], suggested_followups=[])


def _compact(answer: dict[str, Any]) -> str:
    places = ", ".join(loc.get("hub_id") or loc["query"] for loc in answer.get("locations", []))
    return answer.get("answer_markdown", "") + (f"\n[places: {places}]" if places else "")


def confidence_line(text: str) -> str:
    """The last line that states a confidence level ('' if none)."""
    lines = [ln for ln in (text or "").split("\n") if _CONF.search(ln)]
    return lines[-1] if lines else ""


def _fix_confidence_line(text: str, level: str, reason: str | None) -> str:
    """D51: after a failed repair, the code writes the whole line with its own level and reason."""
    line = f"Confidence: {level}" + (f" - {reason}" if reason and level != "high" else "")
    old = confidence_line(text)
    if old:
        return text.replace(old, line)
    return text.rstrip() + f"\n\n{line}"


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

    def _history_allowed_numbers(self, session_id: str, history: list[Message]) -> set[str]:
        """D57: numbers earlier turns make available for grounding. Tool results count in full; user/assistant
        text counts minus any number an earlier answer shipped as unverified (D50), so an unverified number
        can never become 'grounded' by being repeated in a later answer."""
        blocked: set[str] = set()
        for r in self.conn.execute("SELECT answer_json FROM turns WHERE session_id=?", (session_id,)).fetchall():
            blocked |= set(json.loads(r["answer_json"]).get("unverified_numbers", []))
        # Replies to submit_answer (e.g. "rejected: ungrounded numbers [41]") are validator feedback, not data.
        submit_ids = {c["id"] for m in history for c in (m.get("tool_calls") or [])
                      if c.get("function", {}).get("name") == "submit_answer"}
        allowed: set[str] = set()
        for m in history:
            if not isinstance(m.get("content"), str) or m.get("tool_call_id") in submit_ids:
                continue
            nums = numbers_in(m["content"])
            allowed |= nums if m["role"] == "tool" else (nums - blocked)
        return allowed

    def _save(self, session_id: str, user_message: str, full: list[Message], answer: dict[str, Any]) -> None:
        seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE session_id=?", (session_id,)).fetchone()[0]
        self.conn.execute("INSERT INTO turns(session_id, seq, user_message, full_json, answer_json, created_at) "
                          "VALUES (?,?,?,?,?,?)", (session_id, seq, user_message, json.dumps(full), json.dumps(answer),
                                                   datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    # ------------------------------------------------------------------ validation (§5.2, D43, D44)
    def _check(self, arguments: str, sources: list[str], expected_conf: str | None,
               expected_reason: str | None = None, history_allowed: set[str] | frozenset = frozenset()) -> Verdict:
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
        ung = ungrounded_numbers(ans.answer_markdown, sources, history_allowed)
        if ung:
            problems.append(f"ungrounded numbers {ung}: every number must be quoted exactly from a tool result "
                            "(or an earlier answer); remove or replace these")
        conf_wrong = False
        if ans.response_type == "answer" and expected_conf:
            stated = stated_confidence(ans.answer_markdown)
            if stated != expected_conf:
                conf_wrong = True
                problems.append(f"confidence line: end the answer with 'Confidence: {expected_conf} - <short reason>' "
                                f"({expected_conf} is the lowest confidence among this turn's tool results; "
                                f"you wrote {stated or 'none'})")
            elif expected_conf != "high" and expected_reason:
                # D52: the line must explain WHY, with the conflicting facts (their numbers) from the tool result.
                missing = sorted(numbers_in(expected_reason) - numbers_in(confidence_line(ans.answer_markdown)))
                if missing:
                    conf_wrong = True
                    problems.append(f"confidence line must explain why it is {expected_conf}, using these facts "
                                    f"with their numbers: \"{expected_reason}\" (missing: {missing})")
        return Verdict(ans, problems, ung, conf_wrong)

    # ------------------------------------------------------------------ one turn
    def chat(self, message: str, session_id: str | None = None) -> TurnResult:
        t0 = time.perf_counter()
        session_id = session_id or uuid.uuid4().hex[:12]
        history = self._history(session_id)
        sys_prompt = system_prompt(self.today)
        user_msg: Message = {"role": "user", "content": message}
        messages: list[Message] = [{"role": "system", "content": sys_prompt}, *history, user_msg]
        turn_msgs: list[Message] = [user_msg]
        sources = [sys_prompt, message]                       # + this turn's tool results, added below
        history_allowed = self._history_allowed_numbers(session_id, history)
        data_results: list[tuple[str, dict]] = []
        debug: dict[str, Any] = {"rounds": 0, "tool_calls": [], "tool_results": [], "providers": [], "tokens": 0,
                                 "events": []}
        events_before = len(getattr(self.provider, "events", []))
        repairs_left = 1
        final: AgentAnswer | None = None
        verdict: Verdict | None = None

        try:
            for _ in range(self.max_rounds):
                lt = self.provider.complete(messages, TOOLS, "required")
                debug["rounds"] += 1
                debug["tokens"] += lt.usage.get("total", 0)
                debug["providers"].append(f"{lt.provider}:{lt.model}")
                if not lt.tool_calls:   # shouldn't happen with tool_choice="required"; nudge
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
                    debug["tool_results"].append({"tool": call.name, "result": result})
                    if not is_err and call.name in DATA_TOOLS:
                        data_results.append((call.name, result))
                        sources.append(content)
                    tool_msg = {"role": "tool", "tool_call_id": call.id, "content": content}
                    messages.append(tool_msg)
                    turn_msgs.append(tool_msg)
                for call in lt.tool_calls:   # every tool call needs a response, duplicates included
                    if call.name == "submit_answer" and call is not submit:
                        dup = {"role": "tool", "tool_call_id": call.id, "content": '{"error":"duplicate submit_answer"}'}
                        messages.append(dup)
                        turn_msgs.append(dup)
                if submit is None:
                    continue
                levels = [r.get("confidence") for _, r in data_results if r.get("confidence")]
                expected = min(levels, key=lambda c: _LEVELS[c]) if levels else None
                expected_reason = next((r["confidence_reason"] for _, r in data_results
                                        if r.get("confidence") == expected and r.get("confidence_reason")), None)
                verdict = self._check(submit.arguments, sources, expected, expected_reason, history_allowed)
                accept = verdict.ok or (verdict.fixable_by_code and repairs_left == 0)
                reply = {"status": "accepted"} if accept else {
                    "status": "rejected", "problems": verdict.problems, "fix": "correct these and call submit_answer again"}
                submit_reply = {"role": "tool", "tool_call_id": submit.id, "content": json.dumps(reply)}
                messages.append(submit_reply)
                turn_msgs.append(submit_reply)
                if accept:
                    final = verdict.answer
                    if verdict.confidence_wrong and expected:
                        final.answer_markdown = _fix_confidence_line(final.answer_markdown, expected, expected_reason)
                        debug["events"].append(f"confidence line written by code: {expected}")
                    if verdict.ungrounded:   # D50: the code, not the model, writes this warning
                        final.answer_markdown = (final.answer_markdown.rstrip() + "\n\nNote: some numbers in this "
                                                 "answer could not be verified against the data: "
                                                 + ", ".join(verdict.ungrounded) + ".")
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
        levels = [r.get("confidence") for _, r in data_results if r.get("confidence")]
        confidence = min(levels, key=lambda c: _LEVELS[c]) if levels and final.response_type == "answer" else None
        ungrounded = verdict.ungrounded if (verdict and final is verdict.answer) else []
        self._save(session_id, message, turn_msgs, {**answer, "unverified_numbers": ungrounded})
        return TurnResult(session_id, answer, confidence, bool(ungrounded), ungrounded, debug)
