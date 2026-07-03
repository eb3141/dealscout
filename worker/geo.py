"""Free geocoding (Nominatim) and drive times (OSRM demo server).

Every successful lookup is cached forever in SQLite, so a given town or
route costs one HTTP request ever. Nominatim's usage policy is max 1 req/s
with a real User-Agent — enforced below.
"""

import os
import sqlite3
import time
from pathlib import Path

import requests

DEALSCOUT_HOME = Path(os.environ.get("DEALSCOUT_HOME", Path.home() / ".dealscout"))
CACHE_PATH = DEALSCOUT_HOME / "geo.sqlite"
USER_AGENT = "DealScout/1.0 (personal marketplace tool)"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"

_last_nominatim_call = 0.0


def _conn():
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH, timeout=15)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS geocode (place TEXT PRIMARY KEY, lat REAL, lon REAL, ts REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS routes (key TEXT PRIMARY KEY, minutes REAL, ts REAL)"
    )
    return conn


def geocode(place):
    """place -> (lat, lon) or None. Cached forever (including misses as NULLs)."""
    if not place or not place.strip():
        return None
    place = place.strip()
    with _conn() as c:
        row = c.execute("SELECT lat, lon FROM geocode WHERE place=?", (place.lower(),)).fetchone()
    if row:
        return (row[0], row[1]) if row[0] is not None else None

    global _last_nominatim_call
    wait = 1.1 - (time.time() - _last_nominatim_call)
    if wait > 0:
        time.sleep(wait)
    _last_nominatim_call = time.time()

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": place, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None  # transient failure: don't cache, retry next time

    latlon = (float(data[0]["lat"]), float(data[0]["lon"])) if data else (None, None)
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO geocode (place, lat, lon, ts) VALUES (?,?,?,?)",
            (place.lower(), latlon[0], latlon[1], time.time()),
        )
    return latlon if latlon[0] is not None else None


def drive_minutes(origin, dest):
    """(lat,lon) pair -> whole driving minutes, or None if unroutable."""
    if not origin or not dest:
        return None
    key = f"{origin[0]:.4f},{origin[1]:.4f}|{dest[0]:.4f},{dest[1]:.4f}"
    with _conn() as c:
        row = c.execute("SELECT minutes FROM routes WHERE key=?", (key,)).fetchone()
    if row:
        return round(row[0]) if row[0] is not None else None

    url = f"{OSRM_URL}/{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
    try:
        resp = requests.get(
            url,
            params={"overview": "false"},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None

    minutes = None
    if data.get("code") == "Ok" and data.get("routes"):
        minutes = data["routes"][0]["duration"] / 60.0
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO routes (key, minutes, ts) VALUES (?,?,?)",
            (key, minutes, time.time()),
        )
    return round(minutes) if minutes is not None else None


def drive_minutes_to_place(home_latlon, place):
    return drive_minutes(home_latlon, geocode(place))
