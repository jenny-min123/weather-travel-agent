"""
Amap (高德) Web Service client — private module, not mapped as a route.

Endpoints used:
  * /v3/weather/weatherInfo  — live weather (`extensions=base`) and 4-day
    forecast (`extensions=all`)
  * /v3/geocode/geo          — city name → adcode / lon,lat
  * /v3/direction/driving    — driving route (阶段二: 沿途天气采样)

Contract:
  * Every public coroutine raises `UpstreamError` on failure. Tools convert
    that into a `{ok: false, ...}` payload at the boundary; nothing here
    returns a half-populated dict.
  * Amap answers HTTP 200 even for errors — the real status is the string
    field `status` ("1" = OK) plus the numeric `infocode`. Both are checked.
  * Every successful response is cached in-process for `CACHE_TTL` seconds,
    keyed by the full request. The free Amap quota is small, and the same
    city is typically queried several times per conversation turn
    (`get_weather` then `get_travel_advice`).

No credential ever leaves this module: `api_key` is only ever placed in the
outbound query string, and `UpstreamError` messages are built from the
provider's own `info` text, never from the request URL.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ._config import amap_route_key, amap_weather_key

AMAP_BASE = "https://restapi.amap.com"
URL_WEATHER = f"{AMAP_BASE}/v3/weather/weatherInfo"
URL_GEOCODE = f"{AMAP_BASE}/v3/geocode/geo"
URL_DRIVING = f"{AMAP_BASE}/v3/direction/driving"

REQUEST_TIMEOUT = 8.0
CACHE_TTL = 600.0  # seconds

# Amap returns HTTP 200 for domain errors; `status == "1"` is the only success.
_AMAP_OK = "1"

_cache: dict[str, tuple[float, Any]] = {}


class UpstreamError(Exception):
    """An upstream call failed. `code` is provider-supplied when available."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "httpStatus": self.http_status}


def _cache_get(key: str) -> Any | None:
    hit = _cache.get(key)
    if not hit:
        return None
    value, expires_at = hit
    if time.time() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: Any) -> None:
    _cache[key] = (value, time.time() + CACHE_TTL)


async def _get_json(url: str, params: dict[str, str]) -> dict:
    """GET + JSON decode. Raises UpstreamError for transport/parse failures."""
    cache_key = url + "|" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise UpstreamError("NETWORK_ERROR", f"无法连接高德服务：{type(exc).__name__}") from exc

    if resp.status_code != 200:
        raise UpstreamError(
            "HTTP_ERROR",
            f"高德服务返回 HTTP {resp.status_code}",
            http_status=resp.status_code,
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise UpstreamError("BAD_RESPONSE", "高德返回内容不是合法 JSON") from exc

    if not isinstance(payload, dict):
        raise UpstreamError("BAD_RESPONSE", "高德返回结构异常")

    _cache_put(cache_key, payload)
    return payload


def _require_ok(payload: dict, *, what: str) -> None:
    """Raise when Amap reports a domain-level failure."""
    status = str(payload.get("status", "")).strip()
    if status == _AMAP_OK:
        return
    info = str(payload.get("info", "UNKNOWN") or "UNKNOWN")
    infocode = str(payload.get("infocode", "") or "")
    raise UpstreamError(infocode or "AMAP_ERROR", f"高德{what}失败：{info}")


# ========== 实况 / 预报 ==========
def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_live(item: dict) -> dict:
    """Map an Amap `lives[]` entry onto our own field names."""
    return {
        "city": item.get("city", ""),
        "province": item.get("province", ""),
        "adcode": item.get("adcode", ""),
        "weather": item.get("weather", ""),
        "temperature": _to_float(item.get("temperature_float") or item.get("temperature")),
        "humidity": _to_float(item.get("humidity_float") or item.get("humidity")),
        "winddirection": item.get("winddirection", ""),
        "windpower": item.get("windpower", ""),
        "reporttime": item.get("reporttime", ""),
        "source": "amap",
    }


async def weather_live(city: str) -> dict:
    """
    Current conditions for `city` via `/v3/weather/weatherInfo?extensions=base`.

    `city` accepts a name ("北京"), an adcode ("110000"), or "lon,lat".
    Returns the normalized dict described in `_normalize_live`.
    """
    city = (city or "").strip()
    if not city:
        raise UpstreamError("MISSING_CITY", "未提供城市名")

    key = amap_weather_key()
    if not key:
        raise UpstreamError("MISSING_KEY", "未配置 AMAP_WEATHER_API_KEY")

    payload = await _get_json(
        URL_WEATHER,
        {"key": key, "city": city, "extensions": "base", "output": "JSON"},
    )
    _require_ok(payload, what="实况天气查询")

    lives = payload.get("lives") or []
    if not lives:
        raise UpstreamError(
            "EMPTY_LIVES",
            f"高德未返回「{city}」的实况数据（请检查城市名是否为地级市及以上）",
        )

    return _normalize_live(lives[0])


async def weather_forecast(city: str) -> dict:
    """
    4-day forecast via `extensions=all`. Reserved for a later stage; included
    so `_advice.py` can be extended without touching this client.
    """
    key = amap_weather_key()
    if not key:
        raise UpstreamError("MISSING_KEY", "未配置 AMAP_WEATHER_API_KEY")

    payload = await _get_json(
        URL_WEATHER,
        {"key": key, "city": (city or "").strip(), "extensions": "all", "output": "JSON"},
    )
    _require_ok(payload, what="预报查询")

    forecasts = payload.get("forecasts") or []
    if not forecasts:
        raise UpstreamError("EMPTY_FORECAST", f"高德未返回「{city}」的预报数据")

    first = forecasts[0]
    return {
        "city": first.get("city", ""),
        "adcode": first.get("adcode", ""),
        "province": first.get("province", ""),
        "reporttime": first.get("reporttime", ""),
        "casts": [
            {
                "date": c.get("date", ""),
                "week": c.get("week", ""),
                "dayweather": c.get("dayweather", ""),
                "nightweather": c.get("nightweather", ""),
                "daytemp": _to_float(c.get("daytemp")),
                "nighttemp": _to_float(c.get("nighttemp")),
                "daywind": c.get("daywind", ""),
                "daypower": c.get("daypower", ""),
            }
            for c in (first.get("casts") or [])
        ],
        "source": "amap",
    }


# ========== 地理编码 ==========
async def geocode(address: str) -> dict:
    """
    Resolve `address` to `{adcode, city, province, longitude, latitude}`.

    Used to (a) align a free-text city with an adcode and (b) hand a
    longitude/latitude pair to the QWeather hourly endpoint when its own
    city lookup is unavailable.
    """
    address = (address or "").strip()
    if not address:
        raise UpstreamError("MISSING_CITY", "未提供地址")

    key = amap_route_key()
    if not key:
        raise UpstreamError("MISSING_KEY", "未配置 AMAP_ROUTE_API_KEY")

    payload = await _get_json(
        URL_GEOCODE,
        {"key": key, "address": address, "output": "JSON"},
    )
    _require_ok(payload, what="地理编码")

    geocodes = payload.get("geocodes") or []
    if not geocodes:
        raise UpstreamError("EMPTY_GEOCODE", f"高德无法解析地名「{address}」")

    first = geocodes[0]
    location = str(first.get("location", "") or "")
    lon = lat = None
    if "," in location:
        lon_str, lat_str = location.split(",", 1)
        lon, lat = _to_float(lon_str), _to_float(lat_str)

    return {
        "query": address,
        "formatted": first.get("formatted_address", ""),
        "province": first.get("province", "") or "",
        "city": first.get("city", "") or "",
        "adcode": first.get("adcode", "") or "",
        "level": first.get("level", "") or "",
        "longitude": lon,
        "latitude": lat,
        "source": "amap",
    }


# ========== 驾车路径（阶段二：沿途天气） ==========
def _decode_polyline(polyline: str) -> list[tuple[float, float]]:
    """Amap polyline is `"lon,lat;lon,lat;..."` — decode to float pairs."""
    points: list[tuple[float, float]] = []
    for chunk in (polyline or "").split(";"):
        if "," not in chunk:
            continue
        lon_str, lat_str = chunk.split(",", 1)
        lon, lat = _to_float(lon_str), _to_float(lat_str)
        if lon is None or lat is None:
            continue
        points.append((lon, lat))
    return points


async def route_driving(origin: str, destination: str, *, strategy: int = 0) -> dict:
    """
    Driving route between two `"lon,lat"` (or adcode) strings.

    Returns distance/duration plus the flattened polyline, which
    `sample_route_points` turns into evenly spaced waypoints.
    """
    key = amap_route_key()
    if not key:
        raise UpstreamError("MISSING_KEY", "未配置 AMAP_ROUTE_API_KEY")

    payload = await _get_json(
        URL_DRIVING,
        {
            "key": key,
            "origin": origin,
            "destination": destination,
            "strategy": str(strategy),
            "extensions": "base",
            "output": "JSON",
        },
    )
    _require_ok(payload, what="路径规划")

    route = payload.get("route") or {}
    paths = route.get("paths") or []
    if not paths:
        raise UpstreamError("EMPTY_ROUTE", "高德未返回可用驾车路径")

    path = paths[0]
    polyline: list[tuple[float, float]] = []
    for step in path.get("steps") or []:
        polyline.extend(_decode_polyline(step.get("polyline", "")))

    return {
        "origin": origin,
        "destination": destination,
        "distance_m": _to_float(path.get("distance")) or 0.0,
        "duration_s": _to_float(path.get("duration")) or 0.0,
        "toll_m": _to_float(path.get("tolls")) or 0.0,
        "traffic_lights": int(_to_float(path.get("traffic_lights")) or 0),
        "step_count": len(path.get("steps") or []),
        "polyline": polyline,
        "source": "amap",
    }


def sample_route_points(route: dict, n: int) -> list[tuple[float, float]]:
    """
    Evenly sample `n` waypoints (endpoints included) along a decoded polyline.

    Pure function so it is unit-testable without network access.
    """
    polyline: list[tuple[float, float]] = route.get("polyline") or []
    if not polyline:
        return []
    if n <= 1 or len(polyline) == 1:
        return [polyline[0]]

    last = len(polyline) - 1
    picked: list[tuple[float, float]] = []
    for i in range(n):
        idx = round(last * i / (n - 1))
        point = polyline[idx]
        if not picked or picked[-1] != point:
            picked.append(point)
    return picked
