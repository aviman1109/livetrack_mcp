"""Tests for livetrack_mcp.poller — URL parsing and trackpoint fetching."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from livetrack_mcp.poller import TrackPoint, fetch_trackpoints, parse_url


# ── parse_url ──────────────────────────────────────────────────────────────────

class TestParseUrl:
    def test_standard_https_url(self):
        url = "https://livetrack.garmin.com/session/abc123/token/tok456"
        session, viewer = parse_url(url)
        assert session == "abc123"
        assert viewer == "tok456"

    def test_url_with_query_params(self):
        url = "https://livetrack.garmin.com/session/abc123/token/tok456?lang=en"
        session, viewer = parse_url(url)
        assert session == "abc123"
        assert viewer == "tok456"

    def test_url_with_trailing_path(self):
        # viewer token stops at / or ? or #
        url = "https://livetrack.garmin.com/session/abc123/token/tok456/extra"
        session, viewer = parse_url(url)
        assert session == "abc123"
        assert viewer == "tok456"

    def test_long_tokens(self):
        session_tok = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        viewer_tok = "XYZ-very-long-viewer-token-0987654321"
        url = f"https://livetrack.garmin.com/session/{session_tok}/token/{viewer_tok}"
        s, v = parse_url(url)
        assert s == session_tok
        assert v == viewer_tok

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse LiveTrack URL"):
            parse_url("https://connect.garmin.com/something/else")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_url("")

    def test_garmin_livetrack_missing_token_raises(self):
        with pytest.raises(ValueError):
            parse_url("https://livetrack.garmin.com/session/abc123/")


# ── fetch_trackpoints ──────────────────────────────────────────────────────────

SAMPLE_RESPONSE = {
    "trackPoints": [
        {
            "dateTime": "2026-04-13T06:00:00.000Z",
            "position": {"lat": 25.04776, "lon": 121.51714},
            "heartRate": 145,
            "speed": 8.5,
            "power": 220,
            "distance": 1500.0,
            "cadence": 88,
            "altitude": 50.0,
        },
        {
            "dateTime": "2026-04-13T06:01:00.000Z",
            "position": {"lat": 25.04800, "lon": 121.51750},
            "heartRate": 150,
            "speed": 9.0,
            "power": 235,
            "distance": 2040.0,
            "cadence": 90,
            "altitude": 52.0,
        },
    ]
}


def _make_mock_response(data: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


class TestFetchTrackpoints:
    @pytest.mark.asyncio
    async def test_returns_trackpoints(self):
        mock_resp = _make_mock_response(SAMPLE_RESPONSE)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("livetrack_mcp.poller.httpx.AsyncClient", return_value=mock_client):
            points = await fetch_trackpoints("sess123", "view456")

        assert len(points) == 2
        assert isinstance(points[0], TrackPoint)
        assert points[0].hr == 145
        assert points[0].speed_ms == 8.5
        assert points[0].power_w == 220
        assert points[0].lat == pytest.approx(25.04776)
        assert points[0].lon == pytest.approx(121.51714)
        assert points[1].cadence == 90

    @pytest.mark.asyncio
    async def test_empty_response(self):
        mock_resp = _make_mock_response({"trackPoints": []})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("livetrack_mcp.poller.httpx.AsyncClient", return_value=mock_client):
            points = await fetch_trackpoints("sess123", "view456")

        assert points == []

    @pytest.mark.asyncio
    async def test_since_adds_request_time_param(self):
        mock_resp = _make_mock_response({"trackPoints": []})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        since = datetime(2026, 4, 13, 6, 0, 0, tzinfo=timezone.utc)
        with patch("livetrack_mcp.poller.httpx.AsyncClient", return_value=mock_client):
            await fetch_trackpoints("sess123", "view456", since=since)

        call_kwargs = mock_client.get.call_args.kwargs
        assert "requestTime" in call_kwargs["params"]
        assert "2026-04-13T06:00:00.000Z" == call_kwargs["params"]["requestTime"]

    @pytest.mark.asyncio
    async def test_no_since_omits_request_time_param(self):
        mock_resp = _make_mock_response({"trackPoints": []})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("livetrack_mcp.poller.httpx.AsyncClient", return_value=mock_client):
            await fetch_trackpoints("sess123", "view456")

        call_kwargs = mock_client.get.call_args.kwargs
        assert call_kwargs["params"] == {}

    @pytest.mark.asyncio
    async def test_skips_points_with_missing_datetime(self):
        data = {
            "trackPoints": [
                {"dateTime": "", "heartRate": 140},
                {"dateTime": "2026-04-13T06:00:00.000Z", "heartRate": 145},
            ]
        }
        mock_resp = _make_mock_response(data)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("livetrack_mcp.poller.httpx.AsyncClient", return_value=mock_client):
            points = await fetch_trackpoints("sess123", "view456")

        assert len(points) == 1
        assert points[0].hr == 145

    @pytest.mark.asyncio
    async def test_none_fields_for_missing_data(self):
        data = {
            "trackPoints": [
                {"dateTime": "2026-04-13T06:00:00.000Z"},
            ]
        }
        mock_resp = _make_mock_response(data)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("livetrack_mcp.poller.httpx.AsyncClient", return_value=mock_client):
            points = await fetch_trackpoints("sess123", "view456")

        pt = points[0]
        assert pt.hr is None
        assert pt.speed_ms is None
        assert pt.lat is None
        assert pt.lon is None
