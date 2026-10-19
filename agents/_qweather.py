"""
QWeather (和风天气) hourly-forecast client — private module, not a route.

Endpoint used:
  * /v7/weather/24h   — next 24 hours, one entry per hour
  * /geo/v2/city/lookup — city name → QWeather location ID

Notes that matter in production:
  * `QWEATHER_API_HOST` is stored as a **bare host** in `.env`
    (e.g. `nd5u9yjya2.re.qweatherapi.com`), so every URL is built from
    `qweather_base_url()`; never interpolate the host raw.
  * QWeather answers HTTP 200 with a string `code` field ("200" = OK), so the
    HTTP status alone proves nothing.
  * Failures are *soft*: `hourly_forecast` returns `{"available": False, ...}`
    rather than raising, because the hourly panel is an optional enrichment —
    a missing QWeather key must not break the main weather answer.
  * Location resolution degrades gracefully: QWeather's own geo lookup first,
    then an Amap longitude/latitude pair (QWeather accepts `"lng,lat"`).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ._amap import UpstreamError, geocode
from ._config import qweather_base_url, qweather_key, qweather_host

REQUEST_TIMEOUT = 8.0
CACHE_TTL = 300.0  # hourly data refreshes at most once an hour; 5 min is safe

_QWEATHER_OK = "200"

_cache: dict[str, tuple[float, Any]] = {}
_loc_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(store: dict, key: str):
    hit = store.get(key)
    if not hit:
        return None
    value, expires_at = hit
    if time.time() > expires_at:
        store.pop(key, None)
        return None
    return value


def _cache_put(store: dict, key: str, value) -> None:
    store[key] = (value, time.time() + CACHE_TTL)


async def _get_json(url: str, params: dict[str, str]) -> dict:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise UpstreamError("NETWORK_ERROR", f"无法连接和风天气服务：{type(exc).__name__}") from exc

    if resp.status_code != 200:
        raise UpstreamError(
            "HTTP_ERROR",
            f"和风天气返回 HTTP {resp.status_code}",
            http_status=resp.status_code,
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise UpstreamError("BAD_RESPONSE", "和风天气返回内容不是合法 JSON") from exc

    if not isinstance(payload, dict):
        raise UpstreamError("BAD_RESPONSE", "和风天气返回结构异常")
    return payload


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ========== 位置解析 ==========
async def resolve_location(city: str) -> dict:
    """
    Resolve `city` to something QWeather accepts as `location`.

    Returns `{"location": "<id or lng,lat>", "name": ..., "latitude": ..., "longitude": ...}`.
    Raises UpstreamError when neither path works.
    """
    city = (city or "").strip()
    if not city:
        raise UpstreamError("MISSING_CITY", "未提供城市名")

    cached = _cache_get(_loc_cache, city)
    if cached is not None:
        return cached

    base = qweather_base_url()
    key = qweather_key()

    # Path 1 — QWeather's own city lookup (gives a precise location ID).
    if base and key:
        try:
            payload = await _get_json(
                f"{base}/geo/v2/city/lookup",
                {"location": city, "key": key, "number": "1"},
            )
            if str(payload.get("code", "")) == _QWEATHER_OK:
                locations = payload.get("location") or []
                if locations:
                    first = locations[0]
                    resolved = {
                        "location": str(first.get("id", "") or ""),
                        "name": str(first.get("name", "") or city),
                        "adm1": str(first.get("adm1", "") or ""),
                        "adm2": str(first.get("adm2", "") or ""),
                        "latitude": _to_float(first.get("lat")),
                        "longitude": _to_float(first.get("lon")),
                        "resolvedBy": "qweather-geo",
                    }
                    if resolved["location"]:
                        _cache_put(_loc_cache, city, resolved)
                        return resolved
        except UpstreamError:
            # Fall through to the Amap-based path below.
            pass

    # Path 2 — borrow Amap's geocode result and hand QWeather a "lng,lat".
    geo = await geocode(city)
    lon, lat = geo.get("longitude"), geo.get("latitude")
    if lon is None or lat is None:
        raise UpstreamError("LOCATION_UNRESOLVED", f"无法解析「{city}」的经纬度")

    resolved = {
        # QWeather accepts at most 2 decimals for "lng,lat".
        "location": f"{lon:.2f},{lat:.2f}",
        "name": geo.get("city") or city,
        "adm1": geo.get("province", ""),
        "adm2": geo.get("city", ""),
        "latitude": lat,
        "longitude": lon,
        "resolvedBy": "amap-geocode",
    }
    _cache_put(_loc_cache, city, resolved)
    return resolved


# ========== 逐小时预报 ==========
def _normalize_hour(item: dict) -> dict:
    return {
        "time": item.get("fxTime", ""),
        "temperature": _to_float(item.get("temp")),
        "text": item.get("text", ""),
        "icon": item.get("icon", ""),
        "windDir": item.get("windDir", ""),
        "windScale": item.get("windScale", ""),
        "windSpeed": _to_float(item.get("windSpeed")),
        "humidity": _to_float(item.get("humidity")),
        "precip": _to_float(item.get("precip")),
        "pressure": _to_float(item.get("pressure")),
        "cloud": _to_float(item.get("cloud")),
        "pop": _to_float(item.get("pop")),  # probability of precipitation, %
    }


async def hourly_forecast(city: str, hours: int = 12) -> dict:
    """
    Next `hours` hourly entries for `city` (1–24).

    Never raises for provider/transport reasons: returns
    `{"available": False, "reason": ..., "component": "hourly_forecast"}` so
    callers can report a degraded answer instead of failing the whole turn.
    """
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = 12
    hours = max(1, min(24, hours))

    base = qweather_base_url()
    key = qweather_key()
    if not base:
        return {
            "component": "hourly_forecast",
            "available": False,
            "reason": "未配置 QWEATHER_API_HOST，逐小时预报不可用",
        }
    if not key:
        return {
            "component": "hourly_forecast",
            "available": False,
            "reason": "未配置 QWEATHER_API_KEY，逐小时预报不可用",
        }

    try:
        resolved = await resolve_location(city)
    except UpstreamError as exc:
        return {
            "component": "hourly_forecast",
            "available": False,
            "reason": f"无法定位「{city}」：{exc.message}",
        }

    cache_key = f"{resolved['location']}|{hours}"
    cached = _cache_get(_cache, cache_key)
    if cached is not None:
        return cached

    try:
        payload = await _get_json(
            f"{base}/v7/weather/24h",
            {"location": resolved["location"], "key": key},
        )
    except UpstreamError as exc:
        return {
            "component": "hourly_forecast",
            "available": False,
            "reason": f"和风天气请求失败：{exc.message}",
        }

    code = str(payload.get("code", "") or "")
    if code != _QWEATHER_OK:
        return {
            "component": "hourly_forecast",
            "available": False,
            "reason": f"和风天气返回错误码 {code or 'UNKNOWN'}",
        }

    raw_hourly = payload.get("hourly") or []
    if not raw_hourly:
        return {
            "component": "hourly_forecast",
            "available": False,
            "reason": f"和风天气未返回「{city}」的逐小时数据",
        }

    result = {
        "component": "hourly_forecast",
        "available": True,
        "provider": "qweather",
        "city": resolved.get("name") or city,
        "location": resolved["location"],
        "resolvedBy": resolved.get("resolvedBy", ""),
        "updateTime": payload.get("updateTime", ""),
        "hourly": [_normalize_hour(h) for h in raw_hourly[:hours]],
    }
    _cache_put(_cache, cache_key, result)
    return result


def provider_status() -> dict:
    """Non-sensitive diagnostics for the hourly provider."""
    key = qweather_key()
    return {
        "provider": "qweather",
        "host": qweather_host() or "<unset>",
        "key": f"<set,{len(key)} chars>" if key else "<unset>",
        "configured": bool(qweather_host() and key),
    }
