"""Check for meaningful hub score changes and send them to ALERT_WEBHOOK_URL (D76). No LLM, no quota.

    python scripts/check_scores.py             # compare with data/score_baseline.json, notify, update it
    python scripts/check_scores.py --no-save   # notify but keep the baseline (demo: edit a weight, run, revert)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from app.config import ROOT  # noqa: E402
from app.data.http import CachedHttp  # noqa: E402
from app.scoring.alerts import check_score_changes  # noqa: E402
from app.scoring.service import ScoringService  # noqa: E402
from app.store.db import connect  # noqa: E402


def report(result: dict) -> None:
    print(f"status: {result['status']} | webhook: {result['webhook']}")
    if result.get("text"):
        print(result["text"])


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true", help="don't update the baseline file")
    a = ap.parse_args()
    result = check_score_changes(ScoringService(CachedHttp(connect())), os.getenv("ALERT_WEBHOOK_URL"),
                                 save=not a.no_save)
    report(result)
    return 1 if result["webhook"].startswith("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
