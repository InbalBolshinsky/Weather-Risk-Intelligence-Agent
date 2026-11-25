"""SQLite storage: one file, created on first use (PLAN.md §2.3)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import db_path

SCHEMA = """
-- Raw upstream responses. expires_at NULL = never expires (immutable history).
CREATE TABLE IF NOT EXISTS http_cache (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    params      TEXT NOT NULL,
    body        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    expires_at  REAL
);

-- FEMA National Risk Index, all US counties (loaded at refresh; ~3,200 rows).
CREATE TABLE IF NOT EXISTS nri_counties (
    stcofips    TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    county      TEXT NOT NULL,
    nri_ver     TEXT NOT NULL,
    attrs       TEXT NOT NULL,     -- JSON: {HAZ_AFREQ: float|null, HAZ_RISKR: str, ...}
    loaded_at   TEXT NOT NULL
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
