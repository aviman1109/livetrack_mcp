"""Tests for livetrack_mcp.tracker — state machine, race-end detection, deadlock safety."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from livetrack_mcp.tracker import LiveTrackTracker, STALE_STOP_MIN, MIN_ELAPSED_MIN


def _make_store():
    store = MagicMock()
    store.clear = AsyncMock()
    store.kv_set = AsyncMock()
    store.kv_get = AsyncMock(return_value=None)
    store.insert_trackpoints = AsyncMock(return_value=0)
    store.get_recent = AsyncMock(return_value=[])
    store.get_first_point_time = AsyncMock(return_value=None)
    store.get_last_point_time = AsyncMock(return_value=None)
    store.get_total_count = AsyncMock(return_value=0)
    return store


VALID_URL = "https://livetrack.garmin.com/session/sess-abc/token/view-xyz"
RACE_CONFIG = {"poll_interval_secs": 60, "analyze_interval_secs": 600, "analyze_window_min": 10}


# ── start / stop ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_sets_active(tmp_path):
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    result = await tracker.start(VALID_URL, RACE_CONFIG)
    assert result["ok"] is True
    assert tracker.state.active is True
    # clean up
    await tracker.stop()


@pytest.mark.asyncio
async def test_start_rejects_when_already_active(tmp_path):
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    await tracker.start(VALID_URL, RACE_CONFIG)
    result = await tracker.start(VALID_URL, RACE_CONFIG)
    assert result["ok"] is False
    assert "Already tracking" in result["error"]
    await tracker.stop()


@pytest.mark.asyncio
async def test_start_rejects_invalid_url():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    result = await tracker.start("https://connect.garmin.com/bad", RACE_CONFIG)
    assert result["ok"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_stop_sets_inactive():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    await tracker.start(VALID_URL, RACE_CONFIG)
    result = await tracker.stop()
    assert result["ok"] is True
    assert tracker.state.active is False


@pytest.mark.asyncio
async def test_stop_when_not_active_returns_error():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    result = await tracker.stop()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_start_parses_tokens():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    await tracker.start(VALID_URL, RACE_CONFIG)
    assert tracker.state.session_token == "sess-abc"
    assert tracker.state.viewer_token == "view-xyz"
    await tracker.stop()


# ── update_thresholds ─────────────────────────────────────────────────────────

def test_update_thresholds_applies_allowed_keys():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    tracker._state.active = True
    tracker._state.race_config = {"hr_max": 170, "power_max": 250}
    result = tracker.update_thresholds({"hr_max": 175, "power_max": 260})
    assert result["ok"] is True
    assert result["applied"]["hr_max"] == 175
    assert result["applied"]["power_max"] == 260
    assert tracker.state.race_config["hr_max"] == 175


def test_update_thresholds_ignores_unknown_keys():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    tracker._state.race_config = {}
    result = tracker.update_thresholds({"hr_max": 170, "not_a_key": 999})
    assert "not_a_key" in result["ignored"]
    assert "not_a_key" not in tracker.state.race_config


# ── _check_race_end ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_race_end_triggers_stop_when_stale_and_elapsed():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    await tracker.start(VALID_URL, RACE_CONFIG)

    # Set started_at far enough in the past to exceed MIN_ELAPSED_MIN
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    tracker._state.started_at = now - timedelta(minutes=MIN_ELAPSED_MIN + 5)
    tracker._state.last_new_point_at = now - timedelta(minutes=STALE_STOP_MIN + 1)

    await tracker._check_race_end()

    assert tracker.state.active is False


@pytest.mark.asyncio
async def test_check_race_end_does_not_stop_when_not_stale():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    await tracker.start(VALID_URL, RACE_CONFIG)

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    tracker._state.started_at = now - timedelta(minutes=60)
    # Recent point — not stale
    tracker._state.last_new_point_at = now - timedelta(minutes=2)

    await tracker._check_race_end()
    assert tracker.state.active is True
    await tracker.stop()


@pytest.mark.asyncio
async def test_check_race_end_does_not_stop_before_min_elapsed():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    await tracker.start(VALID_URL, RACE_CONFIG)

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    # Short race — elapsed < MIN_ELAPSED_MIN
    tracker._state.started_at = now - timedelta(minutes=5)
    tracker._state.last_new_point_at = now - timedelta(minutes=STALE_STOP_MIN + 5)

    await tracker._check_race_end()
    assert tracker.state.active is True
    await tracker.stop()


# ── stop deadlock safety (current_task check) ─────────────────────────────────

@pytest.mark.asyncio
async def test_stop_from_poll_task_does_not_deadlock():
    """Simulates stop() being called from within the poll task (race-end path).

    The critical invariant: stop() must not cancel-and-await its own task.
    If it does, the test hangs indefinitely.
    """
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    await tracker.start(VALID_URL, RACE_CONFIG)

    # Run stop() inside a task that pretends to be the poll task
    poll_task_ref: list[asyncio.Task] = []

    async def simulate_poll_task_calling_stop():
        # Inject ourselves as the poll task
        tracker._poll_task = asyncio.current_task()
        poll_task_ref.append(asyncio.current_task())
        await tracker.stop(reason="race_end_auto")

    await asyncio.wait_for(
        asyncio.create_task(simulate_poll_task_calling_stop()),
        timeout=2.0,
    )
    assert tracker.state.active is False


# ── get_status ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_status_when_inactive():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    status = tracker.get_status()
    assert status["active"] is False
    assert status["session_token"] is None
    assert status["elapsed_min"] == 0.0


@pytest.mark.asyncio
async def test_get_status_when_active():
    store = _make_store()
    tracker = LiveTrackTracker(store, "http://localhost:38095", "training")
    await tracker.start(VALID_URL, RACE_CONFIG)
    status = tracker.get_status()
    assert status["active"] is True
    assert status["session_token"] is not None
    assert status["elapsed_min"] >= 0.0
    await tracker.stop()
