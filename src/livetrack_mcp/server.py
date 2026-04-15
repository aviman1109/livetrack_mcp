"""LiveTrack MCP server — autonomous Garmin LiveTrack polling + Claude analysis."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .otel import setup_otel
from .store import TrackStore
from .tracker import LiveTrackTracker

# ── Logging ────────────────────────────────────────────────────────────────────

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("livetrack.server")

# ── Config from env ────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("DB_PATH", "/data/livetrack.db")
CLAUDE_RUNNER_URL = os.environ.get("CLAUDE_RUNNER_URL", "http://localhost:38095")
RUNNER_WORKSPACE = os.environ.get("RUNNER_WORKSPACE", "training")
PORT = int(os.environ.get("PORT", "38100"))
HOST = os.environ.get("HOST", "0.0.0.0")
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
_ALLOWED_HOSTS = os.environ.get("MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*")
_ALLOWED_ORIGINS = os.environ.get("MCP_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*")

# ── Singletons ─────────────────────────────────────────────────────────────────

_store = TrackStore(DB_PATH)
_tracker: LiveTrackTracker | None = None


def get_tracker() -> LiveTrackTracker:
    global _tracker
    if _tracker is None:
        _tracker = LiveTrackTracker(_store, CLAUDE_RUNNER_URL, RUNNER_WORKSPACE)
    return _tracker


# ── FastMCP setup ──────────────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = """
LiveTrack MCP autonomously polls a Garmin LiveTrack URL, stores time-series in SQLite,
and triggers Claude analysis via claude-runner every 10 minutes.

## Typical session flow
1. start_tracking(url, race_config) — start polling (auto-analyzes every 10 min)
2. Server polls every 60 s and auto-stops when no new data for 15 min (race finished)
3. get_tracking_status() — check active state, elapsed time, stale time
4. stop_tracking() — manual stop

## race_config fields
- hr_max / hr_min: cycling HR thresholds
- power_max / power_min: ERG power thresholds (watts)
- cadence_min: minimum acceptable cadence (rpm)
- run_hr_max / run_hr_min: run segment HR thresholds
- run_cadence_min: run minimum cadence (spm)
- poll_interval_secs: polling frequency (default: 60)
- analyze_interval_secs: analysis frequency (default: 600)
- analyze_window_min: data window for analysis (default: 10)

## Control API
POST /control {"hr_max": 145, "power_max": 160} — update thresholds mid-race.
Used by Claude's analysis tasks to adjust thresholds without a new MCP call.
""".strip()

mcp = FastMCP(name="LiveTrack MCP", instructions=SERVER_INSTRUCTIONS)

mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[h.strip() for h in _ALLOWED_HOSTS.split(",")],
    allowed_origins=[o.strip() for o in _ALLOWED_ORIGINS.split(",")],
)
mcp.settings.stateless_http = True
mcp.settings.json_response = True
mcp.settings.streamable_http_path = MCP_PATH


# ── MCP Tools ──────────────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Start polling a Garmin LiveTrack share URL. Polling begins immediately "
        "and Claude analysis fires every analyze_interval_secs (default: 600). "
        "race_config must include at minimum: hr_max, power_max (for cycling) or "
        "run_hr_max (for running). Optional keys: hr_min, power_min, cadence_min, "
        "run_hr_min, run_cadence_min, poll_interval_secs, analyze_interval_secs, analyze_window_min."
    )
)
async def start_tracking(url: str, race_config: dict) -> dict:
    return await get_tracker().start(url, race_config)


@mcp.tool(
    description="Stop LiveTrack polling. Server auto-stops when no new data for 15 min (race end). Call manually to stop earlier."
)
async def stop_tracking() -> dict:
    return await get_tracker().stop(reason="manual")


@mcp.tool(
    description="Get current tracking status: active state, elapsed time, last data time, stale time, total points, poll error count.",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def get_tracking_status() -> dict:
    return get_tracker().get_status()


@mcp.tool(
    description="Get recent time-series data from the SQLite store. minutes: how far back to look (1–120, default 10).",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def get_timeseries(minutes: int = 10) -> list[dict]:
    return await _store.get_recent(minutes=max(1, min(minutes, 120)))


@mcp.tool(
    description=(
        "Update race thresholds mid-race. Pass a dict of fields to update. "
        "Allowed keys: hr_max, hr_min, power_max, power_min, cadence_min, "
        "run_hr_max, run_hr_min, run_cadence_min."
    )
)
async def update_thresholds(updates: dict) -> dict:
    return get_tracker().update_thresholds(updates)


@mcp.tool(
    description="Manually trigger Claude analysis of the last analyze_window_min minutes, bypassing the automatic schedule."
)
async def trigger_analysis() -> dict:
    tracker = get_tracker()
    if not tracker.state.active:
        return {"ok": False, "error": "Not tracking"}
    await tracker._analyze_once()
    return {"ok": True}


# ── Custom HTTP routes ─────────────────────────────────────────────────────────

@mcp.custom_route("/control", methods=["POST"])
async def control_endpoint(request: Request) -> JSONResponse:
    """Update race thresholds mid-race via HTTP POST.

    Used by Claude's analysis tasks (curl from Bash tool) so Claude can adjust
    thresholds without needing an MCP session.

    Body: {"hr_max": 145, "power_max": 160}
    Allowed fields: hr_max, hr_min, power_max, power_min, cadence_min,
                    run_hr_max, run_hr_min, run_cadence_min
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    tracker = get_tracker()
    if not tracker.state.active:
        return JSONResponse({"error": "Not currently tracking"}, status_code=409)

    result = tracker.update_thresholds(body)
    log.info("Control API: %s", result)
    return JSONResponse(result)


@mcp.custom_route("/health", methods=["GET"])
async def health_endpoint(request: Request) -> JSONResponse:
    """Health check. Returns tracking state and store stats."""
    tracker = get_tracker()
    status = tracker.get_status()
    return JSONResponse({
        "status": "ok",
        "tracking": status["active"],
        "elapsed_min": status.get("elapsed_min"),
        "total_points": status.get("total_points"),
        "stale_min": status.get("stale_min"),
    })


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    setup_otel()
    log.info("LiveTrack MCP starting on %s:%d (mcp=%s)", HOST, PORT, MCP_PATH)

    async def run() -> None:
        await _store.init()
        get_tracker()  # warm up singleton

        starlette_app = mcp.streamable_http_app()
        config = uvicorn.Config(
            starlette_app,
            host=HOST,
            port=PORT,
            log_level=_LOG_LEVEL.lower(),
        )
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(run())
