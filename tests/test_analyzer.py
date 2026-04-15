"""Tests for livetrack_mcp.analyzer — pure formatting/prompt functions."""
from __future__ import annotations

import pytest

from livetrack_mcp.analyzer import _build_prompt, _format_timeseries


def _make_point(recorded_at_ts: int, hr: int = 145, power_w: int = 220,
                speed_ms: float = 8.5, dist_m: float = 1000.0) -> dict:
    return {
        "recorded_at": recorded_at_ts,
        "hr": hr,
        "power_w": power_w,
        "speed_ms": speed_ms,
        "distance_m": dist_m,
        "cadence": 88,
        "lat": 25.04776,
        "lon": 121.51714,
    }


# ── _format_timeseries ─────────────────────────────────────────────────────────

class TestFormatTimeseries:
    def test_empty_returns_no_data_message(self):
        result = _format_timeseries([])
        assert "no data" in result.lower()

    def test_header_row_present(self):
        pts = [_make_point(1744531200)]  # 2026-04-13T06:00:00Z
        result = _format_timeseries(pts)
        assert "HR" in result
        assert "pwr_W" in result
        assert "spd_kmh" in result

    def test_separator_row_present(self):
        pts = [_make_point(1744531200)]
        result = _format_timeseries(pts)
        assert "---" in result

    def test_data_rows_match_point_count(self):
        pts = [_make_point(1744531200 + i * 60) for i in range(5)]
        result = _format_timeseries(pts)
        lines = result.strip().split("\n")
        # header + separator + 5 data rows = 7 lines
        assert len(lines) == 7

    def test_speed_converted_to_kmh(self):
        pts = [_make_point(1744531200, speed_ms=10.0)]  # 36 km/h
        result = _format_timeseries(pts)
        assert "36.0" in result

    def test_none_fields_show_dash(self):
        pt = {
            "recorded_at": 1744531200,
            "hr": None,
            "power_w": None,
            "speed_ms": None,
            "distance_m": None,
            "cadence": None,
            "lat": None,
            "lon": None,
        }
        result = _format_timeseries([pt])
        # At least one dash for missing field
        assert "-" in result


# ── _build_prompt ─────────────────────────────────────────────────────────────

class TestBuildPrompt:
    def test_contains_elapsed_time(self):
        pts = [_make_point(1744531200)]
        config = {"analyze_window_min": 10}
        prompt = _build_prompt(pts, config, elapsed_min=45.5, control_url="http://localhost:38100/control")
        assert "45.5" in prompt

    def test_contains_race_config_json(self):
        pts = [_make_point(1744531200)]
        config = {"hr_max": 170, "power_max": 250}
        prompt = _build_prompt(pts, config, elapsed_min=10.0, control_url="http://localhost:38100/control")
        assert '"hr_max"' in prompt
        assert '"power_max"' in prompt

    def test_contains_control_url(self):
        pts = [_make_point(1744531200)]
        config = {}
        control = "http://localhost:38100/control"
        prompt = _build_prompt(pts, config, elapsed_min=5.0, control_url=control)
        assert control in prompt

    def test_contains_telegram_instruction(self):
        pts = [_make_point(1744531200)]
        prompt = _build_prompt(pts, {}, elapsed_min=5.0, control_url="http://x/control")
        assert "telegram" in prompt.lower() or "mcp__telegram" in prompt

    def test_window_min_from_config(self):
        pts = [_make_point(1744531200)]
        config = {"analyze_window_min": 15}
        prompt = _build_prompt(pts, config, elapsed_min=30.0, control_url="http://x/control")
        assert "15" in prompt

    def test_point_count_mentioned(self):
        pts = [_make_point(1744531200 + i * 60) for i in range(3)]
        prompt = _build_prompt(pts, {}, elapsed_min=5.0, control_url="http://x/control")
        assert "3" in prompt
