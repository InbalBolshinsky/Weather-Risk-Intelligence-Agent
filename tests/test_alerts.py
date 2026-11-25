"""Score-change alerts (D76): what counts as meaningful, baseline handling, webhook payload. Offline."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.scoring.alerts as alerts
from app.scoring.alerts import check_score_changes, diff
from app.scoring.engine import load_scoring_config


def snap(**hubs):
    """hubs: id -> (rank, {score: value})"""
    return {"config_version": "v1", "data_version": "NRI Dec 2025",
            "hubs": {h: {"label": h.title(), "rank": r, "scores": dict(s)} for h, (r, s) in hubs.items()}}


def test_only_meaningful_changes_alert():
    base = snap(HOU=(3, {"overall": 41, "hurricane": 62, "flood": 78}))
    cur = snap(HOU=(2, {"overall": 44, "hurricane": 70, "flood": 81}))
    changes, new = diff(base, cur, min_points=5, extreme=80)
    got = {c["score"]: c for c in changes}
    assert set(got) == {"hurricane", "flood"}                  # overall +3: below the 5-point tie rule
    assert got["flood"]["extreme"] == "now extreme"            # +3 but crosses 80
    assert got["hurricane"]["rank_from"] == 3 and got["hurricane"]["rank_to"] == 2
    assert new["hubs"]["HOU"]["scores"] == {"overall": 41, "hurricane": 70, "flood": 81}   # overall keeps drifting


def test_small_moves_accumulate_until_meaningful():
    base = snap(DEN=(5, {"overall": 30}))
    changes, new = diff(base, snap(DEN=(5, {"overall": 33})), 5, 80)
    assert changes == []
    changes, _ = diff(new, snap(DEN=(5, {"overall": 36})), 5, 80)
    assert [(c["from"], c["to"]) for c in changes] == [(30, 36)]


def test_new_and_removed_hubs_are_tracked_silently():
    changes, new = diff(snap(OLD=(1, {"overall": 50})), snap(NEW=(1, {"overall": 50})), 5, 80)
    assert changes == [] and list(new["hubs"]) == ["NEW"]


@pytest.fixture
def fake_scoring(monkeypatch):
    state = {"snap": snap(HOU=(3, {"overall": 41, "hurricane": 62}))}
    monkeypatch.setattr(alerts, "snapshot", lambda scoring: json.loads(json.dumps(state["snap"])))
    return SimpleNamespace(cfg=load_scoring_config()), state


class Post:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, url, json, timeout):
        if self.fail:
            raise ConnectionError("down")
        self.sent.append(json)
        return SimpleNamespace(raise_for_status=lambda: None)


def test_check_creates_baseline_then_notifies_once(tmp_path, fake_scoring):
    scoring, state = fake_scoring
    path, post = tmp_path / "baseline.json", Post()
    assert check_score_changes(scoring, "https://hook", path, post=post)["status"] == "baseline_created"
    state["snap"] = snap(HOU=(2, {"overall": 41, "hurricane": 70}))
    r = check_score_changes(scoring, "https://hook", path, post=post)
    assert r["status"] == "changes" and r["webhook"] == "sent" and len(post.sent) == 1
    p = post.sent[0]
    assert p["event"] == "score_change" and p["text"] == p["content"]          # Slack reads text, Discord content
    assert "Hou (HOU) hurricane: 62 -> 70 (overall rank 3 -> 2)" in p["text"]
    assert check_score_changes(scoring, "https://hook", path, post=post)["status"] == "no_changes"   # not twice


def test_failed_send_keeps_baseline_for_retry(tmp_path, fake_scoring):
    scoring, state = fake_scoring
    path = tmp_path / "baseline.json"
    check_score_changes(scoring, None, path)
    state["snap"] = snap(HOU=(3, {"overall": 41, "hurricane": 70}))
    assert check_score_changes(scoring, "https://hook", path, post=Post(fail=True))["webhook"] == "failed: ConnectionError"
    assert check_score_changes(scoring, "https://hook", path, post=Post())["webhook"] == "sent"


def test_no_save_leaves_baseline_untouched(tmp_path, fake_scoring):
    scoring, state = fake_scoring
    path = tmp_path / "baseline.json"
    check_score_changes(scoring, None, path)
    before = path.read_text()
    state["snap"] = snap(HOU=(3, {"overall": 41, "hurricane": 90}))
    assert check_score_changes(scoring, None, path, save=False)["status"] == "changes"
    assert path.read_text() == before
