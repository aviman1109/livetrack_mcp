"""Garmin LiveTrack URL parser and trackpoint fetcher.

Garmin migrated the LiveTrack viewer to a Next.js app in 2026. The old
`/session/{id}/trackpoints` JSON endpoint is gone. The new flow:

  1. GET the viewer page — returns HTML with a `<meta name="csrf-token">` tag
     whose content value must be sent back as the `Livetrack-Csrf-Token` header.
  2. GET `/api/sessions/{id}/track-points/common?token={viewer_token}[&begin=ISO]`
     returns `{"trackPoints": [...]}`. Pagination cursor is `begin` set to the
     last point's `dateTime + 1ms`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("livetrack.poller")

LIVETRACK_BASE = "https://livetrack.garmin.com"
_URL_RE = re.compile(r"livetrack\.garmin\.com/session/([^/]+)/token/([^/?#\s]+)")
_CSRF_META_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class TrackPoint:
    recorded_at: datetime
    lat: float | None
    lon: float | None
    hr: int | None
    speed_ms: float | None
    power_w: int | None
    distance_m: float | None
    cadence: int | None
    altitude_m: float | None
    raw: dict


def parse_url(url: str) -> tuple[str, str]:
    """Extract (session_token, viewer_token) from a Garmin LiveTrack share URL."""
    m = _URL_RE.search(url)
    if not m:
        raise ValueError(
            f"Cannot parse LiveTrack URL: {url!r}\n"
            "Expected format: https://livetrack.garmin.com/session/<token>/token/<viewer_token>"
        )
    return m.group(1), m.group(2)


def _coerce_point(tp: dict) -> TrackPoint | None:
    ts_raw = tp.get("dateTime")
    if not ts_raw:
        return None
    try:
        recorded_at = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except ValueError:
        log.warning("Unparseable dateTime: %r — skipping", ts_raw)
        return None

    pos = tp.get("position") or {}
    return TrackPoint(
        recorded_at=recorded_at,
        lat=pos.get("lat"),
        lon=pos.get("lon"),
        hr=tp.get("heartRateBeatsPerMin"),
        speed_ms=tp.get("speedMetersPerSec") or tp.get("speed"),
        power_w=tp.get("powerWatts") or tp.get("power"),
        distance_m=tp.get("totalDistanceMeters") or tp.get("distanceMeters"),
        cadence=tp.get("cadenceCyclesPerMin"),
        altitude_m=tp.get("altitude"),
        raw=tp,
    )


async def fetch_trackpoints(
    session_token: str,
    viewer_token: str,
    since: datetime | None = None,
) -> list[TrackPoint]:
    """Fetch trackpoints, paginating until the server returns an empty page."""
    viewer_url = f"{LIVETRACK_BASE}/session/{session_token}/token/{viewer_token}"
    api_url = f"{LIVETRACK_BASE}/api/sessions/{session_token}/track-points/common"

    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        warm = await client.get(viewer_url)
        warm.raise_for_status()

        m = _CSRF_META_RE.search(warm.text)
        if not m:
            raise RuntimeError(
                "csrf-token meta tag missing from viewer page — "
                "Garmin may have changed auth again"
            )
        csrf = m.group(1)

        headers = {
            "Accept": "application/json",
            "Referer": viewer_url,
            "Livetrack-Csrf-Token": csrf,
        }

        collected: list[TrackPoint] = []
        begin: str | None = None
        if since:
            begin = (since + timedelta(milliseconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"

        for _page in range(50):  # hard cap to avoid runaway loops
            params: dict[str, str] = {"token": viewer_token}
            if begin:
                params["begin"] = begin

            resp = await client.get(api_url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            page_points = data.get("trackPoints", [])
            if not page_points:
                break

            parsed = [tp for tp in (_coerce_point(p) for p in page_points) if tp]
            collected.extend(parsed)

            last_dt = page_points[-1].get("dateTime")
            if not last_dt:
                break
            last_recorded = datetime.fromisoformat(str(last_dt).replace("Z", "+00:00"))
            begin = (last_recorded + timedelta(milliseconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"

        log.debug(
            "Fetched %d trackpoints from session=%s since=%s",
            len(collected), session_token[:8], since,
        )
        return collected
