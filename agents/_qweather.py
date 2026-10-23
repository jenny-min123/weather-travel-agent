"""
QWeather (和风天气) client — private module, not a route.

Endpoints used:
  * /v7/weather/24h    — next 24 hours, one entry per hour
  * /v7/weather/72h    — next 72 hours (stage 3: cross-day / longer trips)
  * /v7/weather/168h   — next 168 hours, still hourly (7 天逐小时)
  * /v7/weather/7d     — 7-day daily buckets (last-resort tier)
  * /geo/v2/city/lookup — city name → QWeather location ID

Notes that matter in production:
  * `QWEATHER_API_HOST` is stored as a **bare host** in `.env`
    (e.g. `nd5u9yjya2.re.qweatherapi.com`), so every URL is built from
    `qweather_base_url()`; never interpolate the host raw.
  * QWeather answers HTTP 200 with a string `code` field ("200" = OK), so the
    HTTP status alone proves nothing. HTTP 429 (or `code == "429"`) means rate
    limiting and IS retried with exponential backoff.
  * Failures are *soft*: `hourly_forecast` returns `{"available": False, ...}`
    rather than raising, because the hourly panel is an optional enrichment —
    a missing QWeather key must not break the main weather answer.
  * Stage 3 adds `forecast_at()`, which walks the tiers 24h → 72h → 168h → 7d
    and stops at the first one that covers the requested arrival moment. Each
    tier is cached separately, so escalation costs nothing on a repeat query.
  * Location resolution degrades gracefully: QWeather's own geo lookup first,
    then an Amap longitude/latitude pair (QWeather accepts `"lng,lat"`).
  * The v7 hourly endpoints take **no time parameter** — they always answer
    "from now on". Time alignment therefore happens on our side, by matching
    the arrival datetime against the returned `fxTime` buckets.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from ._amap import UpstreamError, geocode, regeo
from ._config import qweather_base_url, qweather_key, qweather_host
from ._geo import format_coords, parse_coords
from ._timealign import (
    forecast_to_live,
    nearest_date,
    nearest_hour,
    parse_fx_time,
    to_local,
)

REQUEST_TIMEOUT = 8.0
CACHE_TTL = 300.0  # hourly data refreshes at most once an hour; 5 min is safe
FORECAST_CACHE_TTL = 600.0  # tier payloads change slowly; 10 min keeps quota low

#: Ordered escalation chain. Cheapest/precise first, widest last.
FORECAST_TIERS: tuple[tuple[str, str], ...] = (
    ("24h", "/v7/weather/24h"),
    ("72h", "/v7/weather/72h"),
    ("168h", "/v7/weather/168h"),
    ("7d", "/v7/weather/7d"),
)

#: HTTP 429 is the documented throttle signal; 5xx is worth one more try.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_S = (0.6, 1.2, 2.4)

_QWEATHER_OK = "200"
#: Codes meaning "this endpoint is not included in the current plan" — the tier
#: is unavailable, so we skip it and keep escalating instead of failing.
_TIER_UNAVAILABLE = {"401", "403", "404"}

_cache: dict[str, tuple[float, Any]] = {}
_loc_cache: dict[str, tuple[float, dict]] = {}
_tier_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(store: dict, key: str):
    hit = store.get(key)
    if not hit:
        return None
    value, expires_at = hit
    if time.time() > expires_at:
        store.pop(key, None)
        return None
    return value


def _cache_put(store: dict, key: str, value, *, ttl: float = CACHE_TTL) -> None:
    store[key] = (value, time.time() + ttl)


async def _get_json(url: str, params: dict[str, str]) -> dict:
    """
    GET + JSON decode, retrying on 429 with exponential backoff.

    Four sample points firing at once can trip a personal key's QPS ceiling, so
    the backoff here is what keeps a whole route from degrading just because it
    was slightly too eager. Jitter avoids two coroutines retrying in lockstep.
    """
    last_status: int | None = None
    last_code: str = ""

    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise UpstreamError("NETWORK_ERROR", f"无法连接和风天气服务：{type(exc).__name__}") from exc

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError as exc:
                raise UpstreamError("BAD_RESPONSE", "和风天气返回内容不是合法 JSON") from exc
            if not isinstance(payload, dict):
                raise UpstreamError("BAD_RESPONSE", "和风天气返回结构异常")

            # QWeather sometimes answers 200 with a body-level 429.
            if str(payload.get("code", "")) == "429" and attempt < _MAX_RETRIES:
                await _backoff(attempt)
                continue
            return payload

        last_status = resp.status_code
        try:
            body = resp.json()
            last_code = str(body.get("code", "") or "") if isinstance(body, dict) else ""
        except ValueError:
            last_code = ""

        if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
            await _backoff(attempt)
            continue

        raise UpstreamError(
            "RATE_LIMITED" if resp.status_code == 429 else "HTTP_ERROR",
            (
                "和风天气接口限流（429），已重试仍失败，请稍后重试"
                if resp.status_code == 429
                else f"和风天气返回 HTTP {resp.status_code}"
            ),
            http_status=resp.status_code,
        )

    raise UpstreamError(
        "RATE_LIMITED",
        f"和风天气重试 {_MAX_RETRIES} 次仍失败（HTTP {last_status or 'unknown'}"
        f"{'，code ' + last_code if last_code else ''}）",
        http_status=last_status,
    )


async def _backoff(attempt: int) -> None:
    delay = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
    await asyncio.sleep(delay + random.uniform(0, 0.25))


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

    # Path 0 — the caller already handed us a coordinate (浏览器定位).
    # QWeather accepts "lng,lat"/"lon,lat" directly (two decimals at most), so no
    # city lookup is needed. A reverse-geocode is still attempted, purely to give
    # the answer a human-readable district name; failure leaves the name empty and
    # the coordinate query goes ahead regardless.
    point = parse_coords(city)
    if point:
        name = ""
        adm1 = adm2 = ""
        try:
            area = await regeo(format_coords(*point))
            name = area.get("district") or area.get("city") or area.get("province") or ""
            adm1 = area.get("province", "")
            adm2 = area.get("city", "")
        except UpstreamError:
            pass
        resolved = {
            "location": f"{point[0]:.2f},{point[1]:.2f}",
            "name": name,
            "adm1": adm1,
            "adm2": adm2,
            "latitude": point[1],
            "longitude": point[0],
            "resolvedBy": "coordinates",
        }
        _cache_put(_loc_cache, city, resolved)
        return resolved

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


# ========== 阶段三：按到达时刻取预报（逐层升级） ==========
def _normalize_daily(item: dict) -> dict:
    """7d bucket → the same field names the hourly normalizer produces."""
    date = str(item.get("fxDate", "") or "")
    return {
        # Synthetic noon timestamp: the daily bucket has no hour, but the
        # downstream code (and `forecast_to_live`) needs *a* moment.
        "time": f"{date}T12:00:00+08:00" if date else "",
        "date": date,
        "text": item.get("textDay", ""),
        "textDay": item.get("textDay", ""),
        "textNight": item.get("textNight", ""),
        "temperature": None,
        "tempMax": _to_float(item.get("tempMax")),
        "tempMin": _to_float(item.get("tempMin")),
        "humidity": _to_float(item.get("humidity")),
        "windDir": item.get("windDirDay", ""),
        "windScale": item.get("windScaleDay", ""),
        "windDirDay": item.get("windDirDay", ""),
        "windScaleDay": item.get("windScaleDay", ""),
        "precip": _to_float(item.get("precip")),
        "pop": None,  # 7d carries no probability-of-precipitation field
    }


async def _fetch_tier(location: str, tier: str, endpoint: str) -> dict | None:
    """
    Fetch one tier and normalize it.

    Returns `None` when the tier simply is not available on this plan (401/403/
    404) — the caller then escalates instead of reporting a failure. Raises
    `UpstreamError` for genuine transport/limit problems.
    """
    cache_key = f"{tier}|{location}"
    cached = _cache_get(_tier_cache, cache_key)
    if cached is not None:
        return cached or None

    base = qweather_base_url()
    key = qweather_key()
    payload = await _get_json(f"{base}{endpoint}", {"location": location, "key": key})
    code = str(payload.get("code", "") or "")

    if code in _TIER_UNAVAILABLE:
        _cache_put(_tier_cache, cache_key, {}, ttl=FORECAST_CACHE_TTL)
        return None
    if code != _QWEATHER_OK:
        raise UpstreamError("QWEATHER_CODE", f"和风天气 {tier} 返回错误码 {code or 'UNKNOWN'}")

    if tier == "7d":
        entries = [_normalize_daily(item) for item in (payload.get("daily") or [])]
    else:
        entries = [_normalize_hour(item) for item in (payload.get("hourly") or [])]

    data = {
        "tier": tier,
        "updateTime": payload.get("updateTime", ""),
        "entries": entries,
    }
    _cache_put(_tier_cache, cache_key, data, ttl=FORECAST_CACHE_TTL)
    return data


async def forecast_at(
    location: str,
    target: Any,
    *,
    semaphore: asyncio.Semaphore | None = None,
    tolerance_min: int = 90,
) -> dict:
    """
    Forecast for `location` at the moment `target` (aware datetime).

    Walks 24h → 72h → 168h → 7d and returns the first tier that covers the
    target within `tolerance_min`. Never raises: an exhausted chain returns
    `{"status": "unavailable", ...}` so the caller can fall back to live
    conditions for that point only.

    `location` should be the waypoint's own `"lon,lat"` (QWeather accepts two
    decimals) — that skips a city-lookup round trip per sample.
    """
    result: dict[str, Any] = {
        "status": "unavailable",
        "tier": "",
        "entry": None,
        "weather": None,
        "time": "",
        "deltaMin": None,
        "error": None,
        "attempts": [],
    }

    base = qweather_base_url()
    key = qweather_key()
    if not base or not key:
        result["error"] = "未配置 QWEATHER_API_HOST / QWEATHER_API_KEY"
        return result

    goal = to_local(target)
    errors: list[str] = []

    for tier, endpoint in FORECAST_TIERS:
        try:
            if semaphore is not None:
                async with semaphore:
                    data = await _fetch_tier(location, tier, endpoint)
            else:
                data = await _fetch_tier(location, tier, endpoint)
        except UpstreamError as exc:
            errors.append(f"{tier}:{exc.code}")
            result["attempts"].append({"tier": tier, "error": exc.code})
            continue

        if not data:
            result["attempts"].append({"tier": tier, "skipped": "not_available"})
            continue

        entries = data.get("entries") or []
        if not entries:
            result["attempts"].append({"tier": tier, "skipped": "empty"})
            continue

        if tier == "7d":
            match = nearest_date(entries, goal)
            if not match:
                result["attempts"].append({"tier": tier, "missed": "date"})
                continue
            result.update(
                {
                    "status": "daily_fallback",
                    "tier": tier,
                    "entry": match["entry"],
                    "weather": forecast_to_live(match["entry"], tier=tier),
                    "time": match["entry"].get("time", ""),
                    "deltaMin": None,
                }
            )
            result["attempts"].append({"tier": tier, "hit": "date"})
            return result

        match = nearest_hour(entries, goal, tolerance_min=tolerance_min)
        if not match:
            result["attempts"].append({"tier": tier, "missed": "hour"})
            continue

        result.update(
            {
                "status": "hourly",
                "tier": tier,
                "entry": match["entry"],
                "weather": forecast_to_live(match["entry"], tier=tier),
                "time": str(match["entry"].get("time", "")),
                "deltaMin": match["deltaMin"],
            }
        )
        result["attempts"].append({"tier": tier, "hit": "hour", "deltaMin": match["deltaMin"]})
        return result

    if errors:
        result["error"] = "；".join(errors)
    elif not result["attempts"]:
        result["error"] = "和风天气未返回可用预报"
    else:
        result["error"] = "所有预报层均未覆盖该到达时刻"
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
