"""API tests (offline): access code, caps, status, rankings, page. The LLM is a scripted fake."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.llm.base import LLMTurn, ToolCall

ANSWER = {"response_type": "out_of_scope", "intent": "other", "answer_markdown": "I only cover US weather risk.",
          "locations": [], "clarifying_question": None, "clarification_options": [],
          "suggested_followups": ["Which hubs are riskiest?"]}


class OneShot:
    """Fake model: always submits an out-of-scope answer in one round (no tools, no numbers)."""
    name = "fake"
    events: list = []

    def complete(self, messages, tools, tool_choice):
        return LLMTurn(None, [ToolCall("c1", "submit_answer", json.dumps(ANSWER))], "tool_calls", "fake", "fake", {})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WRA_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("DEMO_TOKEN", "s3cret")
    import importlib

    import app.main as main
    importlib.reload(main)                       # fresh module state: new DB path, new thread-local connections
    monkeypatch.setattr(main, "DAILY_CAP", 3)
    monkeypatch.setattr(main, "CONVERSATION_CAP", 2)
    with TestClient(main.create_app(provider_factory=OneShot)) as c:
        yield c


def test_page_and_docs_are_open(client):
    assert client.get("/").status_code == 200 and "Weather Risk Agent" in client.get("/").text
    assert client.get("/docs").status_code == 200
    assert client.get("/icon.png").headers["content-type"] == "image/png"


def test_chat_requires_the_access_code(client):
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "access_code"
    ok = client.post("/api/chat", json={"message": "hi"}, headers={"X-Access-Code": "s3cret"})
    assert ok.status_code == 200 and ok.json()["answer"] == ANSWER["answer_markdown"]
    assert "debug" in ok.json() and ok.json()["questions_left_today"] == 2


def test_conversation_and_daily_caps(client):
    h = {"X-Access-Code": "s3cret"}
    sid = client.post("/api/chat", json={"message": "a"}, headers=h).json()["session_id"]
    client.post("/api/chat", json={"message": "b", "session_id": sid}, headers=h)
    r = client.post("/api/chat", json={"message": "c", "session_id": sid}, headers=h)
    assert r.status_code == 429 and r.json()["detail"]["error"] == "conversation_limit"
    client.post("/api/chat", json={"message": "d"}, headers=h)           # 3rd counted question today
    r = client.post("/api/chat", json={"message": "e"}, headers=h)
    assert r.status_code == 429 and r.json()["detail"]["error"] == "daily_limit"


def test_non_ascii_access_code_is_a_401_not_a_500(client):   # review: compare_digest TypeError
    r = client.post("/api/chat", json={"message": "hi"}, headers={"X-Access-Code": "café".encode()})
    assert r.status_code == 401


def test_question_no_model_answered_is_not_counted(client, monkeypatch):   # review: refund on provider failure
    import app.main as main
    from app.agent.loop import Agent, TurnResult
    monkeypatch.setattr(Agent, "chat", lambda self, m, s=None: TurnResult(
        "s1", {"answer_markdown": "busy", "response_type": "error"}, None, False, [], {}, provider_failed=True))
    r = client.post("/api/chat", json={"message": "hi"}, headers={"X-Access-Code": "s3cret"})
    assert r.status_code == 200 and r.json()["questions_left_today"] == main.DAILY_CAP


def test_alert_check_requires_the_code_and_loaded_data(client):   # D76
    assert client.post("/api/alerts/check").status_code == 401
    r = client.post("/api/alerts/check", headers={"X-Access-Code": "s3cret"})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "data_not_loaded"   # test DB has no hub data


def test_bad_session_id_rejected(client):
    r = client.post("/api/chat", json={"message": "a", "session_id": "../etc"}, headers={"X-Access-Code": "s3cret"})
    assert r.status_code == 400


def test_status_reports_caps_and_access(client):
    s = client.get("/api/status").json()
    assert s["daily_cap"] == 3 and s["access_code_required"] is True and s["questions_left_today"] == 3
