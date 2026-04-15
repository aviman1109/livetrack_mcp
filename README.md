# livetrack-mcp

Autonomous MCP server that polls Garmin LiveTrack, stores time-series in SQLite, and triggers Claude analysis every 10 minutes via claude-runner.

## Architecture

```
Garmin LiveTrack URL
    │
    │  (poll every 60 s)
    ▼
livetrack-mcp (port 38100)
    ├── poller.py      — fetch trackpoints from Garmin API
    ├── store.py       — SQLite time-series persistence (/data/livetrack.db)
    ├── tracker.py     — asyncio scheduling + race-end detection
    └── analyzer.py    — build prompt, call claude-runner
         │
         │  POST /run (fire-and-forget)
         ▼
    claude-runner (port 38095)
         │
         │  claude -p <analysis prompt>
         ▼
    Claude (sonnet)
         ├── analyze timeseries
         ├── curl POST /control if thresholds need adjustment
         └── mcp__telegram__send_message → coaching push
```

**Key design:** livetrack-mcp is fully autonomous — no Claude session needs to stay alive during the race. Claude is called as a stateless analysis function every 10 minutes. If claude-runner is temporarily unavailable, the next analysis cycle retries automatically.

## MCP Tools

| Tool | Description |
|---|---|
| `start_tracking(url, race_config)` | Start polling a LiveTrack share URL |
| `stop_tracking()` | Stop polling (also auto-stops at race end) |
| `get_tracking_status()` | Active state, elapsed time, stale time, poll errors |
| `get_timeseries(minutes=10)` | Recent data from SQLite |
| `update_thresholds(updates)` | Update HR/power thresholds mid-race |
| `trigger_analysis()` | Manual on-demand analysis, bypassing schedule |

## Custom HTTP Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/control` | POST | Update thresholds mid-race (called by Claude via curl in Bash tool) |
| `/health` | GET | Health check — tracking state + store stats |

### `/control` usage (from Claude's analysis prompt)

```bash
curl -sf -X POST http://localhost:38100/control \
  -H 'Content-Type: application/json' \
  -d '{"power_max": 150}'
```

Allowed fields: `hr_max`, `hr_min`, `power_max`, `power_min`, `cadence_min`, `run_hr_max`, `run_hr_min`, `run_cadence_min`

## `race_config` Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `hr_max` | int | — | Cycling HR ceiling (bpm) |
| `hr_min` | int | — | Cycling HR floor |
| `power_max` | int | — | ERG power ceiling (watts) |
| `power_min` | int | — | ERG power floor |
| `cadence_min` | int | — | Minimum cycling cadence (rpm) |
| `run_hr_max` | int | — | Run HR ceiling |
| `run_hr_min` | int | — | Run HR floor |
| `run_cadence_min` | int | — | Minimum run cadence (spm) |
| `poll_interval_secs` | int | 60 | How often to poll LiveTrack |
| `analyze_interval_secs` | int | 600 | How often to trigger Claude analysis |
| `analyze_window_min` | int | 10 | Data window passed to Claude (minutes) |

### Example `race_config` for a full triathlon

```json
{
  "race_name": "CT2026",
  "race_type": "triathlon",
  "hr_max": 144,
  "hr_min": 115,
  "power_max": 165,
  "cadence_min": 82,
  "run_hr_max": 152,
  "run_hr_min": 125,
  "run_cadence_min": 165,
  "poll_interval_secs": 60,
  "analyze_interval_secs": 600,
  "analyze_window_min": 10
}
```

## Race-End Detection

The server auto-stops when:
- No new trackpoints for ≥ 15 minutes (`STALE_STOP_MIN`)
- AND total elapsed time ≥ 30 minutes (`MIN_ELAPSED_MIN`)

This handles the Garmin 24-hour URL delay: the URL remains valid after the race, but new trackpoints stop arriving when the athlete finishes. The 30-minute minimum prevents false stops at the start when GPS data is sparse.

## Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `PORT` | `38100` | Server port |
| `HOST` | `0.0.0.0` | Bind address |
| `MCP_PATH` | `/mcp` | MCP endpoint path |
| `DB_PATH` | `/data/livetrack.db` | SQLite database path |
| `CLAUDE_RUNNER_URL` | `http://localhost:38095` | claude-runner base URL |
| `RUNNER_WORKSPACE` | `training` | Workspace for claude-runner tasks |
| `LOG_LEVEL` | `INFO` | Logging level |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OpenTelemetry collector URL (optional) |

## Deployment

```bash
cd ~/ai-platform/mcps

# Build and start
docker compose up -d --build livetrack-mcp

# Logs
docker compose logs -f livetrack-mcp

# Restart
docker compose restart livetrack-mcp

# Health check
curl http://localhost:38100/health
```

## Typical Session (via Claude in training workspace)

```
# Start tracking
use_mcp_tool livetrack-mcp start_tracking \
  url="https://livetrack.garmin.com/session/.../token/..." \
  race_config={"hr_max": 144, "power_max": 165, "run_hr_max": 152}

# Check status
use_mcp_tool livetrack-mcp get_tracking_status

# Manual analysis trigger
use_mcp_tool livetrack-mcp trigger_analysis

# Stop (or let it auto-stop)
use_mcp_tool livetrack-mcp stop_tracking
```

## Project Structure

```
livetrack_mcp/
├── Dockerfile
├── pyproject.toml
├── README.md
└── src/livetrack_mcp/
    ├── __init__.py
    ├── __main__.py
    ├── otel.py        # OpenTelemetry setup
    ├── poller.py      # Garmin LiveTrack URL parsing + HTTP fetch
    ├── store.py       # SQLite time-series (sqlite3 + asyncio.to_thread)
    ├── tracker.py     # Scheduling (asyncio.create_task) + race-end detection
    ├── analyzer.py    # Prompt builder + claude-runner caller
    └── server.py      # FastMCP tools + /control + /health
```
