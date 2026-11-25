"""Build the deployable data snapshot (D72): data/app.db -> data/snapshot.db.gz.

Keeps only what the app needs to answer from cache: the FEMA county table and the cached upstream
responses for the 20 hubs (2016-2025 baseline weather + station data, geocoding, NRI lookups).
Drops conversations, usage counters, live alerts, and long station windows (refetchable for free).

    python scripts/make_snapshot.py
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DATA_DIR, db_path  # noqa: E402

SNAPSHOT = DATA_DIR / "snapshot.db.gz"


def main() -> int:
    src = sqlite3.connect(db_path())
    if src.execute("SELECT COUNT(*) FROM nri_counties").fetchone()[0] < 3000:
        print("cache is not warm: run scripts/refresh_data.py first")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "snapshot.db"
        src.execute(f"VACUUM INTO '{raw}'")
        c = sqlite3.connect(raw)
        c.execute("DELETE FROM turns")
        c.execute("DELETE FROM usage_counters")
        c.execute("DELETE FROM http_cache WHERE source = 'nws-alerts'")
        c.execute("""DELETE FROM http_cache WHERE source = 'noaa-ghcn' AND params NOT LIKE '%"startDate": "2016-01-01"%'""")
        c.commit()
        c.execute("VACUUM")
        c.close()
        with open(raw, "rb") as f, gzip.open(SNAPSHOT, "wb", compresslevel=9) as g:
            shutil.copyfileobj(f, g)
    print(f"wrote {SNAPSHOT.relative_to(DATA_DIR.parent)} ({SNAPSHOT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
