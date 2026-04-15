"""LiveTrack tracking engine — polling + analysis scheduling + race-end detection."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .analyzer import trigger_analysis
from .poller import TrackPoint, fetch_trackpoints, parse_url
from .store import TrackStore

log = logging.getLogger("livetrack.tracker")

# Race-end detection: stop if no new data for this long AND minimum elapsed
STALE_STOP_MIN: float = 15.0    # minutes without new data → assume finished
MIN_ELAPSED_MIN: float = 30.0   # minimum race time before stale-stop kicks in


@dataclass
class TrackerState:
    active: bool = False
    session_token: str = ""
    viewer_token: str = ""
    race_config: dict = field(default_factory=dict)
    started_at: datetime | None = None
    last_poll_at: datetime | None = None
    last_new_point_at: datetime | None = None
    total_points: int = 0
    poll_errors: int = 0
    last_analysis_at: datetime | None = None


class LiveTrackTracker:
    def __init__(self, store: TrackStore, runner_url: str, workspace: str) -> None:
        self._store = store
        self._runner_url = runner_url
        self._workspace = workspace
        self._state = TrackerState()
        self._poll_task: asyncio.Task | None = None
        self._analyze_task: asyncio.Task | None = None

    @property
    def state(self) -> TrackerState:
        return self._state

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self, url: str, race_config: dict) -> dict:
        if self._state.active:
            return {"ok": False, "error": "Already tracking. Call stop_tracking first."}

        try:
            session_token, viewer_token = parse_url(url)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        await self._store.clear()
        await self._store.kv_set("url", url)
        await self._store.kv_set("race_config", json.dumps(race_config))

        poll_secs = int(race_config.get("poll_interval_secs", 60))
        analyze_secs = int(race_config.get("analyze_interval_secs", 600))

        self._state = TrackerState(
            active=True,
            session_token=session_token,
            viewer_token=viewer_token,
            race_config=race_config,
            started_at=datetime.now(timezone.utc),
        )

        self._poll_task = asyncio.create_task(
            self._poll_loop(poll_secs), name="livetrack-poll"
        )
        self._analyze_task = asyncio.create_task(
            self._analyze_loop(analyze_secs), name="livetrack-analyze"
        )

        log.info(
            "Tracking started: session=%s... poll=%ds analyze=%ds race_config=%s",
            session_token[:8], poll_secs, analyze_secs,
            json.dumps({k: v for k, v in race_config.items() if k != "segments"}, ensure_ascii=False),
        )
        return {
            "ok": True,
            "session_token": session_token,
            "poll_interval_secs": poll_secs,
            "analyze_interval_secs": analyze_secs,
        }

    async def stop(self, reason: str = "manual") -> dict:
        if not self._state.active:
            return {"ok": False, "error": "Not tracking"}

        self._state.active = False

        # current_task() is the task calling stop() — don't cancel-and-await
        # ourselves (deadlock). When stop() is called from within _poll_loop
        # (via _check_race_end), the poll task is the current task; it exits
        # naturally after _poll_once() returns because the while-loop checks
        # self._state.active. External callers (MCP tool / control endpoint)
        # have no current task or a different task, so they can await normally.
        current = asyncio.current_task()

        for task in (self._poll_task, self._analyze_task):
            if task and not task.done() and task is not current:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._poll_task = None
        self._analyze_task = None

        log.info(
            "Tracking stopped: reason=%s total_points=%d elapsed=%.1f min",
            reason, self._state.total_points, self._elapsed_min(),
        )
        return {"ok": True, "reason": reason, "total_points": self._state.total_points}

    def get_status(self) -> dict:
        s = self._state
        now = datetime.now(timezone.utc)
        return {
            "active": s.active,
            "session_token": s.session_token[:8] + "..." if s.session_token else None,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "elapsed_min": round(self._elapsed_min(), 1),
            "last_poll_at": s.last_poll_at.isoformat() if s.last_poll_at else None,
            "last_new_point_at": s.last_new_point_at.isoformat() if s.last_new_point_at else None,
            "stale_min": round(
                (now - s.last_new_point_at).total_seconds() / 60, 1
            ) if s.last_new_point_at else None,
            "total_points": s.total_points,
            "poll_errors": s.poll_errors,
            "last_analysis_at": s.last_analysis_at.isoformat() if s.last_analysis_at else None,
            "race_config": s.race_config,
        }

    def update_thresholds(self, updates: dict) -> dict:
        """Update race_config thresholds. Called by /control endpoint or MCP tool."""
        allowed = {
            "hr_max", "hr_min", "power_max", "power_min",
            "cadence_min", "run_hr_max", "run_hr_min", "run_cadence_min",
        }
        applied: dict[str, Any] = {}
        ignored: dict[str, Any] = {}
        for k, v in updates.items():
            if k in allowed:
                self._state.race_config[k] = v
                applied[k] = v
            else:
                ignored[k] = v

        if applied:
            log.info("Thresholds updated: %s", applied)
        if ignored:
            log.warning("Unknown threshold keys ignored: %s", list(ignored))
        return {"ok": True, "applied": applied, "ignored": ignored}

    # ── Background loops ───────────────────────────────────────────────────────

    async def _poll_loop(self, interval_secs: int) -> None:
        """Poll Garmin LiveTrack every interval_secs until tracking stops."""
        log.debug("Poll loop started: interval=%ds", interval_secs)
        while self._state.active:
            await asyncio.sleep(interval_secs)
            if not self._state.active:
                break
            await self._poll_once()

    async def _analyze_loop(self, interval_secs: int) -> None:
        """Trigger Claude analysis every interval_secs, starting after first interval."""
        log.debug("Analyze loop started: interval=%ds", interval_secs)
        await asyncio.sleep(interval_secs)
        while self._state.active:
            await self._analyze_once()
            await asyncio.sleep(interval_secs)

    # ── Poll logic ─────────────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        s = self._state
        log.debug("Polling LiveTrack: session=%s...", s.session_token[:8])
        try:
            points = await fetch_trackpoints(
                s.session_token,
                s.viewer_token,
                since=s.last_new_point_at,
            )
            s.last_poll_at = datetime.now(timezone.utc)
            s.poll_errors = 0

            if points:
                inserted = await self._store.insert_trackpoints(points)
                if inserted > 0:
                    newest = max(p.recorded_at for p in points)
                    s.last_new_point_at = newest
                    s.total_points += inserted
                    last_hr = next((p.hr for p in reversed(points) if p.hr), None)
                    last_pwr = next((p.power_w for p in reversed(points) if p.power_w), None)
                    last_spd = next((p.speed_ms for p in reversed(points) if p.speed_ms), None)
                    log.info(
                        "Poll OK: +%d pts (total=%d) HR=%s pwr=%sW spd=%s km/h",
                        inserted, s.total_points,
                        last_hr, last_pwr,
                        f"{last_spd * 3.6:.1f}" if last_spd else None,
                    )
                else:
                    log.debug("Poll: %d pts returned, 0 new (duplicates)", len(points))
            else:
                log.debug("Poll: no trackpoints returned")

            await self._check_race_end()

        except Exception as exc:
            s.poll_errors += 1
            log.warning("Poll error #%d: %s", s.poll_errors, exc)

    async def _check_race_end(self) -> None:
        s = self._state
        if not s.last_new_point_at:
            return
        now = datetime.now(timezone.utc)
        stale_min = (now - s.last_new_point_at).total_seconds() / 60
        elapsed_min = self._elapsed_min()

        if stale_min >= STALE_STOP_MIN and elapsed_min >= MIN_ELAPSED_MIN:
            log.info(
                "Race-end detected: stale=%.1f min, elapsed=%.1f min → auto-stopping",
                stale_min, elapsed_min,
            )
            await self.stop(reason="race_end_auto")

    # ── Analysis logic ─────────────────────────────────────────────────────────

    async def _analyze_once(self) -> None:
        if not self._state.active:
            return
        s = self._state
        window_min = s.race_config.get("analyze_window_min", 10)

        log.info("Triggering analysis (elapsed=%.1f min)", self._elapsed_min())
        try:
            recent = await self._store.get_recent(minutes=window_min)
            first_ts = await self._store.get_first_point_time()
            elapsed_min = 0.0
            if first_ts:
                elapsed_min = (datetime.now(timezone.utc) - first_ts).total_seconds() / 60

            result = await trigger_analysis(
                runner_url=self._runner_url,
                workspace=self._workspace,
                timeseries=recent,
                race_config=s.race_config,
                elapsed_min=elapsed_min,
            )
            if not result.get("skipped"):
                s.last_analysis_at = datetime.now(timezone.utc)
                log.info("Analysis task_id=%s", result.get("task_id"))
        except Exception as exc:
            log.error("Analysis trigger failed: %s", exc)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _elapsed_min(self) -> float:
        if not self._state.started_at:
            return 0.0
        return (datetime.now(timezone.utc) - self._state.started_at).total_seconds() / 60
