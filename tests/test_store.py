"""Tests for livetrack_mcp.store — SQLite persistence."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from livetrack_mcp.poller import TrackPoint
from livetrack_mcp.store import TrackStore


def _make_point(ts: datetime, hr: int = 140, speed: float = 8.0,
                power: int = 200, dist: float = 1000.0) -> TrackPoint:
    return TrackPoint(
        recorded_at=ts,
        lat=25.04,
        lon=121.51,
        hr=hr,
        speed_ms=speed,
        power_w=power,
        distance_m=dist,
        cadence=88,
        altitude_m=50.0,
        raw={"test": True},
    )


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.db"
    s = TrackStore(str(db))
    return s


@pytest.mark.asyncio
async def test_init_creates_tables(store, tmp_path):
    await store.init()
    assert Path(store._db_path).exists()


@pytest.mark.asyncio
async def test_insert_and_count(store):
    await store.init()
    pts = [
        _make_point(datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc), dist=0.0),
        _make_point(datetime(2026, 4, 13, 6, 1, 0, tzinfo=timezone.utc), dist=500.0),
    ]
    inserted = await store.insert_trackpoints(pts)
    assert inserted == 2
    assert await store.get_total_count() == 2


@pytest.mark.asyncio
async def test_insert_deduplicates(store):
    await store.init()
    ts = datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc)
    pt = _make_point(ts)

    first = await store.insert_trackpoints([pt])
    second = await store.insert_trackpoints([pt])
    assert first == 1
    assert second == 0
    assert await store.get_total_count() == 1


@pytest.mark.asyncio
async def test_insert_empty_list(store):
    await store.init()
    result = await store.insert_trackpoints([])
    assert result == 0


@pytest.mark.asyncio
async def test_get_recent_returns_in_order(store):
    await store.init()
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    pts = [
        _make_point(now - timedelta(minutes=3)),
        _make_point(now - timedelta(minutes=2), dist=500.0),
        _make_point(now - timedelta(minutes=1), dist=1000.0),
    ]
    await store.insert_trackpoints(pts)
    recent = await store.get_recent(minutes=10)
    assert len(recent) == 3
    # oldest first
    assert recent[0]["recorded_at"] < recent[1]["recorded_at"]
    assert recent[1]["recorded_at"] < recent[2]["recorded_at"]


@pytest.mark.asyncio
async def test_get_last_point_time_returns_max(store):
    await store.init()
    t1 = datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 13, 6, 5, 0, tzinfo=timezone.utc)
    await store.insert_trackpoints([_make_point(t1), _make_point(t2, dist=2000.0)])
    last = await store.get_last_point_time()
    assert last is not None
    assert last.replace(microsecond=0) == t2.replace(microsecond=0)


@pytest.mark.asyncio
async def test_get_first_point_time_returns_min(store):
    await store.init()
    t1 = datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 13, 6, 5, 0, tzinfo=timezone.utc)
    await store.insert_trackpoints([_make_point(t1), _make_point(t2, dist=2000.0)])
    first = await store.get_first_point_time()
    assert first is not None
    assert first.replace(microsecond=0) == t1.replace(microsecond=0)


@pytest.mark.asyncio
async def test_get_extreme_returns_none_when_empty(store):
    await store.init()
    assert await store.get_last_point_time() is None
    assert await store.get_first_point_time() is None


@pytest.mark.asyncio
async def test_kv_set_and_get(store):
    await store.init()
    await store.kv_set("session_url", "https://example.com")
    value = await store.kv_get("session_url")
    assert value == "https://example.com"


@pytest.mark.asyncio
async def test_kv_get_default_when_missing(store):
    await store.init()
    value = await store.kv_get("nonexistent", default="fallback")
    assert value == "fallback"


@pytest.mark.asyncio
async def test_kv_get_none_when_missing_no_default(store):
    await store.init()
    value = await store.kv_get("nonexistent")
    assert value is None


@pytest.mark.asyncio
async def test_kv_set_overwrites(store):
    await store.init()
    await store.kv_set("key", "old_value")
    await store.kv_set("key", "new_value")
    assert await store.kv_get("key") == "new_value"


@pytest.mark.asyncio
async def test_clear_wipes_all_data(store):
    await store.init()
    await store.insert_trackpoints([
        _make_point(datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc))
    ])
    await store.kv_set("url", "https://example.com")
    await store.clear()
    assert await store.get_total_count() == 0
    assert await store.kv_get("url") is None
