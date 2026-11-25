"""Agent loop + provider chain tests with a scripted fake model (offline, free).

Covers: submit_answer validation, grounding (D44) with one repair then the code-written note (D50),
the confidence-line check (D43), schema failure handling, history compaction (D27), and the
fallback chain incl. the single wait when every provider is rate-limited (D40).
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.agent import loop as loop_mod
from app.agent.loop import Agent, stated_confidence, ungrounded_numbers
from app.agent.tools import ToolContext, trim_to_data
from app.data.http import CachedHttp
from app.data.series import DailySeries
from app.llm.base import LLMTurn, MalformedToolCall, ProviderUnavailable, ToolCall
from app.llm.fallback import AllProvidersFailed, FallbackProvider
from app.scoring.service import ScoringService
from app.store.db import connect

PROFILE = {"profiles": [{"place": {"query": "Dallas", "status": "hub_exact", "hub_id": "DAL"}, "wdri": 32,
                         "hazards": {"severe_convective": {"score": 65}}}], "confidence": "medium"}


def answer(text, rtype="answer", **kw):
    a = {"response_type": rtype, "intent": "explain", "answer_markdown": text,
         "locations": [{"query": "Dallas", "hub_id": "DAL"}],
         "clarifying_question": None, "clarification_options": [], "suggested_followups": []}
    a.update(kw)
    return json.dumps(a)


class Script:
    """Fake provider: returns pre-scripted turns in order and records the messages it saw."""
    name = "fake"

    def __init__(self, *turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []
        self.events: list[str] = []

    def complete(self, messages, tools, tool_choice):
        self.seen.append(json.loads(json.dumps(messages)))
        t = self.turns.pop(0)
        if isinstance(t, Exception):
            raise t
        return t


def call(name, args, i=1):
    return LLMTurn(None, [ToolCall(f"c{i}", name, json.dumps(args) if not isinstance(args, str) else args)],
                   "tool_calls", "fake", "fake-model", {"total": 100})


@pytest.fixture
def agent_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "run_tool", lambda ctx, name, args: (PROFILE, False))
    http = CachedHttp(connect(tmp_path / "t.db"))
    ctx = ToolContext(http, ScoringService(http), date(2026, 11, 25))
    return lambda provider: Agent(provider, ctx)


# --- validation ------------------------------------------------------------------

def test_grounded_answer_with_correct_confidence_is_accepted(agent_factory):
    p = Script(call("get_risk_profile", {"places": []}, 1),
               call("submit_answer", answer("Dallas WDRI 32; severe storms 65.\nConfidence: medium - county data"), 2))
    r = agent_factory(p).chat("Why is Dallas high?")
    assert r.answer["response_type"] == "answer" and not r.unverified and r.confidence == "medium"
    assert r.debug["rounds"] == 2 and r.debug["tool_results"][0]["result"] == PROFILE


def test_ungrounded_number_triggers_one_repair_then_succeeds(agent_factory):
    p = Script(call("get_risk_profile", {"places": []}, 1),
               call("submit_answer", answer("Dallas WDRI 41.\nConfidence: medium - x"), 2),
               call("submit_answer", answer("Dallas WDRI 32.\nConfidence: medium - x"), 3))
    r = agent_factory(p).chat("Why is Dallas high?")
    assert not r.unverified and "32" in r.answer["answer_markdown"]
    assert any("repair requested" in e for e in r.debug["events"])


def test_still_ungrounded_after_repair_gets_code_written_note(agent_factory):   # D50
    bad = answer("Dallas WDRI 41.\nConfidence: medium - x")
    p = Script(call("get_risk_profile", {"places": []}, 1), call("submit_answer", bad, 2), call("submit_answer", bad, 3))
    r = agent_factory(p).chat("Why is Dallas high?")
    assert r.unverified and r.ungrounded_numbers == ["41"]
    assert r.answer["answer_markdown"].endswith("could not be verified against the data: 41.")


def test_wrong_confidence_level_is_repaired_then_corrected_by_code(agent_factory):   # D43
    wrong = answer("Dallas WDRI 32.\nConfidence: high - x")
    p = Script(call("get_risk_profile", {"places": []}, 1), call("submit_answer", wrong, 2), call("submit_answer", wrong, 3))
    r = agent_factory(p).chat("Why is Dallas high?")
    assert stated_confidence(r.answer["answer_markdown"]) == "medium"


LOW = {"profiles": [{"place": {"query": "Phoenix", "status": "hub_exact", "hub_id": "PHX"}, "wdri": 28}],
       "confidence": "low",
       "confidence_reason": "PHX: Flood: FEMA records flooding: 8.5 events/yr in Maricopa, AZ (#3 of 20), "
                            "but at the hub days with >= 2 in rain: 0.0 days/yr (#19 of 20)"}


@pytest.fixture
def low_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "run_tool", lambda ctx, name, args: (LOW, False))
    http = CachedHttp(connect(tmp_path / "t.db"))
    return lambda p: Agent(p, ToolContext(http, ScoringService(http), date(2026, 11, 25)))


def test_low_confidence_line_must_carry_the_conflicting_facts(low_agent):   # D52
    vague = answer("Phoenix WDRI 28.\nConfidence: low - sources disagree")
    good = answer("Phoenix WDRI 28.\nConfidence: low - FEMA records 8.5 events/yr in the county (#3 of 20) but the "
                  "hub sees 0.0 days/yr with >= 2 in rain (#19 of 20)")
    p = Script(call("get_risk_profile", {"places": []}, 1), call("submit_answer", vague, 2), call("submit_answer", good, 3))
    r = low_agent(p).chat("How exposed is Phoenix to flooding?")
    assert any("must explain why" in e for e in r.debug["events"])
    assert "8.5" in r.answer["answer_markdown"] and "Phoenix WDRI 28." in r.answer["answer_markdown"]


def test_code_writes_the_full_confidence_line_after_failed_repair(low_agent):   # D51
    vague = answer("Phoenix WDRI 28.\nConfidence: low - sources disagree")
    p = Script(call("get_risk_profile", {"places": []}, 1), call("submit_answer", vague, 2), call("submit_answer", vague, 3))
    text = low_agent(p).chat("How exposed is Phoenix to flooding?").answer["answer_markdown"]
    assert text.startswith("Phoenix WDRI 28.")                       # conclusion unchanged
    assert text.endswith("Confidence: low - " + LOW["confidence_reason"])


def test_schema_failure_twice_returns_clean_error(agent_factory):
    p = Script(call("submit_answer", '{"response_type": "answer"}', 1), call("submit_answer", "{}", 2))
    r = agent_factory(p).chat("hi")
    assert r.answer["response_type"] == "error"


def test_out_of_scope_needs_no_confidence_line(agent_factory):
    p = Script(call("submit_answer", answer("I only cover US weather risk.", rtype="out_of_scope", locations=[]), 1))
    r = agent_factory(p).chat("Write a poem")
    assert r.answer["response_type"] == "out_of_scope" and r.confidence is None


def test_history_keeps_latest_turn_in_full_and_older_turns_compact(agent_factory):   # D27
    ok = lambda i: call("submit_answer", answer("Dallas WDRI 32.\nConfidence: medium - x"), i)   # noqa: E731
    p = Script(call("get_risk_profile", {"places": []}, 1), ok(2),
               call("get_risk_profile", {"places": []}, 3), ok(4),
               ok(5))
    agent = agent_factory(p)
    sid = agent.chat("Q1").session_id
    agent.chat("Q2", sid)
    agent.chat("Q3", sid)
    last = p.seen[-1]
    tool_msgs = [m for m in last if m["role"] == "tool"]
    assert len(tool_msgs) == 2   # only the latest finished turn (Q2) keeps its tool exchange
    assert [m["content"] for m in last if m["role"] == "user"] == ["Q1", "Q2", "Q3"]


def test_unverified_number_cannot_be_grounded_by_repeating_it_later(agent_factory):   # D57
    bad = answer("Dallas WDRI 41.\nConfidence: medium - x")
    repeat = answer("As I said, Dallas WDRI 41.", rtype="answer")
    p = Script(call("get_risk_profile", {"places": []}, 1), call("submit_answer", bad, 2), call("submit_answer", bad, 3),
               call("submit_answer", repeat, 4), call("submit_answer", repeat, 5))
    agent = agent_factory(p)
    first = agent.chat("Why is Dallas high?")
    assert first.unverified and first.ungrounded_numbers == ["41"]
    second = agent.chat("Repeat that?", first.session_id)
    assert second.unverified and second.ungrounded_numbers == ["41"]   # still flagged, not laundered via history


def test_verified_numbers_from_earlier_answers_still_count(agent_factory):
    ok = answer("Dallas WDRI 32.\nConfidence: medium - x")
    p = Script(call("get_risk_profile", {"places": []}, 1), call("submit_answer", ok, 2),
               call("submit_answer", answer("Yes, 32.", rtype="answer"), 3))
    agent = agent_factory(p)
    sid = agent.chat("Why is Dallas high?").session_id
    assert not agent.chat("Was it 32?", sid).unverified


def test_grounding_has_no_small_number_exemption():   # D44
    assert ungrounded_numbers("ranked 3rd of 7", ["rank 3 of 20"]) == ["7"]


# --- provider chain (D13, D40) -----------------------------------------------------

class Fake:
    def __init__(self, name, *outcomes):
        self.name, self.outcomes, self.calls = name, list(outcomes), 0

    def complete(self, messages, tools, tool_choice):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


OK = LLMTurn("ok", [], "stop", "x", "m", {})


def test_fallback_on_unavailable_and_retry_once_on_malformed():
    a = Fake("a", MalformedToolCall("bad"), MalformedToolCall("bad"))
    b = Fake("b", OK)
    assert FallbackProvider([a, b]).complete([], [], "required") is OK
    assert a.calls == 2 and b.calls == 1


def test_waits_once_when_every_provider_is_rate_limited():
    slept: list[float] = []
    a = Fake("a", ProviderUnavailable("rl", retry_after=12), OK)
    b = Fake("b", ProviderUnavailable("rl", retry_after=20))
    assert FallbackProvider([a, b], sleep=slept.append).complete([], [], "required") is OK
    assert slept == [12]


def test_no_wait_when_a_failure_is_not_a_rate_limit():
    slept: list[float] = []
    a = Fake("a", ProviderUnavailable("timeout"))
    with pytest.raises(AllProvidersFailed):
        FallbackProvider([a], sleep=slept.append).complete([], [], "required")
    assert slept == []


# --- weather-stat window trimming (D45) ---------------------------------------------

def test_trim_to_data_cuts_trailing_missing_days():
    s = DailySeries("t", ["d1", "d2", "d3", "d4"], {"snowfall": [0.0, 1.0, None, None]}, {}, {})
    trimmed, last = trim_to_data(s, "snowfall")
    assert last == "d2" and trimmed.dates == ["d1", "d2"] and trimmed.coverage("snowfall") == 1.0
