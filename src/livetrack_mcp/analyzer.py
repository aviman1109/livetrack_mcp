"""Fires a Claude analysis task via claude-runner for the last N minutes of data."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger("livetrack.analyzer")

CONTROL_URL = "http://localhost:38100/control"


def _format_timeseries(points: list[dict]) -> str:
    """Compact tabular representation of trackpoints for the Claude prompt."""
    if not points:
        return "(no data in window)"

    rows = ["time(UTC)  | HR  | pwr_W | spd_kmh | dist_m  | cad | lat      | lon"]
    rows.append("-" * 75)
    for p in points:
        ts = datetime.fromtimestamp(p["recorded_at"], tz=timezone.utc).strftime("%H:%M:%S")
        spd = f"{p['speed_ms'] * 3.6:.1f}" if p["speed_ms"] else "-"
        dist = f"{round(p['distance_m'])}" if p["distance_m"] else "-"
        lat = f"{p['lat']:.5f}" if p["lat"] else "-"
        lon = f"{p['lon']:.5f}" if p["lon"] else "-"
        rows.append(
            f"{ts}   | {p['hr'] or '-':>3} | {p['power_w'] or '-':>5} | "
            f"{spd:>7} | {dist:>7} | {p['cadence'] or '-':>3} | {lat} | {lon}"
        )
    return "\n".join(rows)


def _build_prompt(
    timeseries: list[dict],
    race_config: dict,
    elapsed_min: float,
    control_url: str,
) -> str:
    table = _format_timeseries(timeseries)
    config_json = json.dumps(race_config, ensure_ascii=False, indent=2)
    n_points = len(timeseries)
    window_min = race_config.get("analyze_window_min", 10)

    return f"""你是賽日 LiveTrack 教練。以下是最近 {window_min} 分鐘的時序數據（{n_points} 筆），分析後用 telegram MCP 推播教練建議。

## 比賽進行時間
已進行 {elapsed_min:.1f} 分鐘

## 閾值設定（race_config）
```json
{config_json}
```

## 最近 {window_min} 分鐘時序資料
```
{table}
```

## 分析任務

**Step 1 — 閾值判斷（不輸出到 Telegram）**
根據 HR / 功率趨勢判斷是否需要調整閾值：
- 10 分鐘平均 HR ≥ hr_max - 5 → power_max 降 10W
- 最高 HR > hr_max → power_max 降 15W
- 10 分鐘平均 HR ≤ hr_max - 15 且平均功率距 power_max ≥ 30W → power_max 可調高 5W（前 30 分鐘不調高）
- 跑步段（speed > 2 m/s 且 power_w 為空）：HR 超 run_hr_max 持續 → run_hr_max 調高 3
若需調整，呼叫以下 curl（Bash tool）：
```bash
curl -sf -X POST {control_url} \\
  -H 'Content-Type: application/json' \\
  -d '{{"power_max": <新值>}}'
```
可調欄位：hr_max, hr_min, power_max, power_min, cadence_min, run_hr_max, run_hr_min, run_cadence_min

**Step 2 — Telegram 推播**
呼叫 mcp__telegram__send_message，chat_id=8209401698（Casper DM，不帶 topic_id）。
格式（純文字無符號，Siri 播報友好）：
第一行：HH:MM 距離 (進度%) 段落
第二行：HR 均值 | 功率均值 或 速度均值 | 踏頻均值
第三至五行：2-3 句教練建議，繁體中文，句子短，口語，像教練在耳邊講話

範例：
09:42 45.2km (25%) 單車
HR 138 | 功率 162W | 踏頻 88rpm
節奏很好，繼續維持。踏頻再稍微提高一點，用轉速而不是力量。前段省的力，後段回來。
"""


async def trigger_analysis(
    runner_url: str,
    workspace: str,
    timeseries: list[dict],
    race_config: dict,
    elapsed_min: float,
) -> dict:
    """Submit a fire-and-forget analysis task to claude-runner.

    Returns the runner response dict (task_id, status).
    """
    if not timeseries:
        log.info("Analysis skipped: no data in window")
        return {"skipped": True, "reason": "no_data"}

    prompt = _build_prompt(timeseries, race_config, elapsed_min, CONTROL_URL)

    payload = {
        "workspace": workspace,
        "prompt": prompt,
        "model": "haiku",
        "allowed_tools": "mcp__telegram__send_message,Bash",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{runner_url}/run", json=payload)
        resp.raise_for_status()
        result = resp.json()

    log.info(
        "Analysis task submitted: task_id=%s elapsed=%.1f min points=%d",
        result.get("task_id"), elapsed_min, len(timeseries),
    )
    return result
