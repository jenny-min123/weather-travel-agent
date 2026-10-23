"""
Coordinate helpers — private module: no route, no network, no side effects.

Why this module exists
----------------------
The browser Geolocation API (`navigator.geolocation.getCurrentPosition`) returns
**WGS84**. Amap's `/v3/geocode/regeo` and `/v3/direction/*` expect **GCJ-02**.
Inside mainland China the two differ by roughly 300–500 m — enough to
reverse-geocode the neighbouring street, or to drop a route origin on the wrong
side of a river. 阶段四（浏览器定位）therefore converts once, here.

Coordinate-system convention — IMPORTANT
----------------------------------------
`agents/chat/index.py` converts WGS84 → GCJ-02 **exactly once**, at the request
boundary. Everything downstream (`_tools`, `_amap`, `_route`) treats any
`"lon,lat"` string as GCJ-02. Never convert again: a second pass shifts the
point by another few hundred metres.

QWeather is the one consumer that nominally wants WGS84, but its `location`
parameter accepts at most two decimals (~1 km), so the GCJ-02 value is passed
through unchanged — the difference is far below its own resolution.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "describe",
    "format_coords",
    "normalize_user_location",
    "parse_coords",
    "to_gcj02",
    "wgs84_to_gcj02",
]

# --- GCJ-02 (Krasovsky 1940 ellipsoid) ---------------------------------------
_A = 6378245.0
_EE = 0.00669342162296594323

# Rough mainland-China bounding box. Outside it WGS84 == GCJ-02 (the transform
# would otherwise corrupt overseas coordinates).
_LON_MIN, _LON_MAX = 72.004, 137.8347
_LAT_MIN, _LAT_MAX = 0.8293, 55.8271


def parse_coords(text: Any) -> tuple[float, float] | None:
    """
    Parse a literal `"lon,lat"` string into `(lon, lat)`; return None otherwise.

    Accepts spaces around the separator and rejects out-of-range values, so a
    city name like "北京" or a malformed "113,22,53" simply returns None and the
    caller keeps its name-based path.
    """
    if not isinstance(text, str):
        return None
    parts = text.replace("，", ",").split(",")
    if len(parts) != 2:
        return None
    try:
        lon, lat = float(parts[0].strip()), float(parts[1].strip())
    except (TypeError, ValueError):
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    # 0,0 is the classic "no fix" placeholder — not a usable location.
    if abs(lon) < 1e-6 and abs(lat) < 1e-6:
        return None
    return lon, lat


def format_coords(lon: float, lat: float, digits: int = 6) -> str:
    """`(lon, lat)` → the `"lon,lat"` shape Amap and QWeather both accept."""
    return f"{lon:.{digits}f},{lat:.{digits}f}"


def out_of_china(lon: float, lat: float) -> bool:
    return not (_LON_MIN <= lon <= _LON_MAX and _LAT_MIN <= lat <= _LAT_MAX)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    """
    WGS84 → GCJ-02 ("火星坐标"), the datum Amap speaks.

    Returns the input unchanged outside mainland China, where the two datums
    coincide and the offset formula would introduce a fake displacement.
    """
    if out_of_china(lon, lat):
        return lon, lat

    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)

    rad_lat = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)

    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)

    return lon + dlon, lat + dlat


def normalize_user_location(raw: Any) -> dict | None:
    """
    Validate the `userLocation` field of a /chat request body.

    Tolerant of the shapes a frontend might send — `{lon, lat}`,
    `{lng, latitude}`, numbers as strings — because a rejected location must
    degrade to "ask the user for a city", never to a 500.

    Returns `{"lon", "lat", "accuracy", "coordSys"}` or None.
    `coordSys` is "wgs84" | "gcj02"; the caller converts to GCJ-02 when needed.
    """
    if not isinstance(raw, dict):
        return None

    def _num(*names: str) -> float | None:
        for name in names:
            value = raw.get(name)
            if value is None or isinstance(value, bool):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    lon = _num("lon", "lng", "longitude")
    lat = _num("lat", "latitude")
    if lon is None or lat is None:
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    if abs(lon) < 1e-6 and abs(lat) < 1e-6:
        return None

    accuracy = _num("accuracy", "acc")
    if accuracy is not None and (accuracy < 0 or accuracy > 100_000):
        accuracy = None

    coord_sys = str(raw.get("coordSys") or raw.get("coord_sys") or "wgs84").strip().lower()
    if coord_sys not in ("wgs84", "gcj02"):
        coord_sys = "wgs84"  # browser default; converting twice is the worse error

    return {
        "lon": round(lon, 6),
        "lat": round(lat, 6),
        "accuracy": round(accuracy, 1) if accuracy is not None else None,
        "coordSys": coord_sys,
    }


def to_gcj02(location: dict) -> dict:
    """Return a copy of a `normalize_user_location()` result in GCJ-02."""
    if location.get("coordSys") == "gcj02":
        return dict(location)
    lon, lat = wgs84_to_gcj02(float(location["lon"]), float(location["lat"]))
    return {**location, "lon": round(lon, 6), "lat": round(lat, 6), "coordSys": "gcj02"}


def describe(location: dict) -> str:
    """Human/model-readable one-liner. Never includes anything but coordinates."""
    acc = location.get("accuracy")
    acc_text = f"，精度约 ±{acc:.0f}m" if isinstance(acc, (int, float)) else ""
    return f"经度={location['lon']:.6f}, 纬度={location['lat']:.6f}{acc_text}"
