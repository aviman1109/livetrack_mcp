"""SQLite persistence for LiveTrack time-series data.

Uses sqlite3 + asyncio.to_thread to stay non-blocking without adding aiosqlite dep.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("livetrack.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trackpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at INTEGER NOT NULL UNIQUE,   -- unix timestamp from Garmin data
    polled_at   INTEGER NOT NULL,          -- unix timestamp when we received it
    lat         REAL,
    lon         REAL,
    hr          INTEGER,
    speed_ms    REAL,
    power_w     INTEGER,
    distance_m  REAL,
    cadence     INTEGER,
    altitude_m  REAL,
    raw_json    TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_tp_recorded_at ON trackpoints(recorded_at);
"""


class TrackStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)
        log.info("SQLite store ready: %s", self._db_path)

    def _init_sync(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(SCHEMA)

    async def clear(self) -> None:
        """Wipe all rows — called at the start of a new tracking session."""
        await asyncio.to_thread(self._clear_sync)
        log.info("Store cleared for new session")

    def _clear_sync(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM trackpoints")
            conn.execute("DELETE FROM kv")

    # ── Trackpoints ────────────────────────────────────────────────────────────

    async def insert_trackpoints(self, points: list) -> int:
        """Insert new trackpoints, silently ignore duplicates. Returns count inserted."""
        if not points:
            return 0
        return await asyncio.to_thread(self._insert_sync, points)

    def _insert_sync(self, points: list) -> int:
        now = int(datetime.now(timezone.utc).timestamp())
        inserted = 0
        with sqlite3.connect(self._db_path) as conn:
            before = conn.total_changes
            for p in points:
                ts = int(p.recorded_at.timestamp())
                conn.execute(
                    """INSERT OR IGNORE INTO trackpoints
                       (recorded_at, polled_at, lat, lon, hr, speed_ms, power_w,
                        distance_m, cadence, altitude_m, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, now, p.lat, p.lon, p.hr, p.speed_ms, p.power_w,
                     p.distance_m, p.cadence, p.altitude_m, json.dumps(p.raw)),
                )
            inserted = conn.total_changes - before
        return inserted

    async def get_recent(self, minutes: int = 10) -> list[dict]:
        """Return trackpoints from the last N minutes, oldest first."""
        since_ts = int(datetime.now(timezone.utc).timestamp()) - minutes * 60
        return await asyncio.to_thread(self._get_recent_sync, since_ts)

    def _get_recent_sync(self, since_ts: int) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trackpoints WHERE recorded_at >= ? ORDER BY recorded_at ASC",
                (since_ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def get_last_point_time(self) -> datetime | None:
        """Timestamp of the most recent trackpoint, or None if empty."""
        return await asyncio.to_thread(self._get_extreme_sync, "MAX")

    async def get_first_point_time(self) -> datetime | None:
        """Timestamp of the earliest trackpoint (race start), or None if empty."""
        return await asyncio.to_thread(self._get_extreme_sync, "MIN")

    def _get_extreme_sync(self, func: str) -> datetime | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {func}(recorded_at) FROM trackpoints"
            ).fetchone()
        if row and row[0]:
            return datetime.fromtimestamp(row[0], tz=timezone.utc)
        return None

    async def get_total_count(self) -> int:
        return await asyncio.to_thread(self._count_sync)

    def _count_sync(self) -> int:
        with sqlite3.connect(self._db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM trackpoints").fetchone()[0]

    # ── Key-Value (session metadata) ───────────────────────────────────────────

    async def kv_set(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._kv_set_sync, key, value)

    def _kv_set_sync(self, key: str, value: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value)
            )

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        return await asyncio.to_thread(self._kv_get_sync, key, default)

    def _kv_get_sync(self, key: str, default: str | None) -> str | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default
