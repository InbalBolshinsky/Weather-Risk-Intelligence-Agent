"""Score-change alerts (bonus, D76). Deterministic, no LLM.

Scores use a fixed data window, so they change only when the data is refreshed or the scoring config changes.
`check_score_changes` recomputes the 20 hubs, compares them with the last ALERTED values in the committed
baseline file (data/score_baseline.json; it survives restarts of the free host), and POSTs the meaningful
changes to ALERT_WEBHOOK_URL. Meaningful = a score moves >= 5 points (the tie rule) or a hazard crosses the
extreme mark (80): the same rules the agent uses. Smaller moves accumulate until they cross the threshold.
Triggers: the end of scripts/refresh_data.py, scripts/check_scores.py, and POST /api/alerts/check.
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import DATA_DIR
from app.scoring.engine import ScoringConfig
from app.scoring.service import ScoringService

BASELINE_PATH = DATA_DIR / "score_baseline.json"
_LOCK = threading.Lock()


def snapshot(scoring: ScoringService) -> dict[str, Any]:
    """Current display-rounded scores and overall ranks of all hubs."""
    cfg = scoring.cfg
    digits = cfg.display.score

    def rnd(x: float) -> float | int:
        return int(round(x)) if digits == 0 else round(x, digits)

    scores = scoring.all_hub_scores()
    ranks = {e.key: e.rank for e in scoring.rank("overall").entries}
    return {
        "config_version": cfg.version,
        "data_version": scores[0].data_version if scores else "",
        "hubs": {s.key: {"label": s.label, "rank": ranks[s.key],
                         "scores": {"overall": rnd(s.wdri)} | {h: rnd(r.score) for h, r in s.hazards.items()}}
                 for s in scores},
    }


def diff(baseline: dict[str, Any], current: dict[str, Any], min_points: float,
         extreme: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Meaningful changes, and the new baseline (only the alerted values move; small drifts keep accumulating)."""
    new = copy.deepcopy(baseline)
    new["config_version"], new["data_version"] = current["config_version"], current["data_version"]
    new["hubs"] = {k: v for k, v in new.get("hubs", {}).items() if k in current["hubs"]}   # hubs removed from config
    changes: list[dict[str, Any]] = []
    for hub, cur in current["hubs"].items():
        old = new["hubs"].get(hub)
        if old is None:                                    # a hub added to config: start tracking, no alert
            new["hubs"][hub] = copy.deepcopy(cur)
            continue
        changed = False
        for key, to in cur["scores"].items():
            frm = old["scores"].get(key)
            if frm is None:
                old["scores"][key] = to
                continue
            crossed = key != "overall" and (frm >= extreme) != (to >= extreme)
            if abs(to - frm) >= min_points or crossed:
                changes.append({"hub": hub, "label": cur["label"], "score": key, "from": frm, "to": to,
                                "rank_from": old["rank"], "rank_to": cur["rank"],
                                "extreme": ("now extreme" if to >= extreme else "no longer extreme") if crossed else None})
                old["scores"][key] = to
                changed = True
        if changed:
            old["rank"] = cur["rank"]
    return changes, new


def summary_text(baseline: dict[str, Any], current: dict[str, Any], changes: list[dict[str, Any]],
                 cfg: ScoringConfig) -> str:
    """Readable message (plain text: renders the same in Slack, Discord, email)."""
    why = []
    if baseline.get("config_version") != current["config_version"]:
        why.append(f"scoring config {baseline.get('config_version')} -> {current['config_version']}")
    if baseline.get("data_version") != current["data_version"]:
        why.append(f"data {baseline.get('data_version')} -> {current['data_version']}")
    lines = [f"Weather risk scores changed ({'; '.join(why) or 'data refresh'})"]
    for c in changes:
        name = "overall" if c["score"] == "overall" else cfg.hazards[c["score"]].label.lower()
        line = f"- {c['label']} ({c['hub']}) {name}: {c['from']} -> {c['to']}"
        if c["extreme"]:
            line += f", {c['extreme']} (>= {cfg.flags.extreme_hazard_score:g})"
        if c["rank_from"] != c["rank_to"]:
            line += f" (overall rank {c['rank_from']} -> {c['rank_to']})"
        lines.append(line)
    return "\n".join(lines)


def check_score_changes(scoring: ScoringService, webhook_url: str | None, path: Path = BASELINE_PATH,
                        save: bool = True, post: Callable[..., Any] = httpx.post) -> dict[str, Any]:
    """Compare with the baseline; notify; update the baseline unless a webhook send failed (retried next check)."""
    with _LOCK:
        current = snapshot(scoring)
        if not path.exists():
            if save:
                _write(path, current)
            return {"status": "baseline_created", "changes": [], "webhook": "not sent"}
        baseline = json.loads(path.read_text())
        cfg = scoring.cfg
        changes, new = diff(baseline, current, cfg.ranking.tie_points, cfg.flags.extreme_hazard_score)
        result: dict[str, Any] = {"status": "changes" if changes else "no_changes", "changes": changes,
                                  "webhook": "not sent"}
        if changes:
            text = summary_text(baseline, current, changes, cfg)
            result["text"] = text
            if webhook_url:
                payload = {"text": text, "content": text[:2000], "event": "score_change",
                           "config_version": current["config_version"], "data_version": current["data_version"],
                           "changes": changes}
                try:
                    post(webhook_url, json=payload, timeout=10).raise_for_status()
                    result["webhook"] = "sent"
                except Exception as e:  # noqa: BLE001 - report it; keep the baseline so the next check retries
                    result["webhook"] = f"failed: {type(e).__name__}"
                    return result
            else:
                result["webhook"] = "not configured (ALERT_WEBHOOK_URL)"
        if save and new != baseline:
            _write(path, new)
        return result


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
