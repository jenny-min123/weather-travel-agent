"""
Amap (高德) Web Service client — private module, not mapped as a route.

Endpoints used:
  * /v3/weather/weatherInfo          — live weather (`extensions=base`) and
    4-day forecast (`extensions=all`)
  * /v3/geocode/geo                  — city name → adcode / lon,lat
  * /v3/geocode/regeo                — lon,lat → district / adcode (阶段二)
  * /v3/direction/driving            — driving route (阶段二: 沿途天气采样)
  * /v3/direction/transit/integrated — public transport route (阶段二)
  * /v3/direction/walking            — walking route (阶段二)

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

import asyncio
import math
import time
from typing import Any

import httpx

from ._config import amap_route_key, amap_weather_key

AMAP_BASE = "https://restapi.amap.com"
URL_WEATHER = f"{AMAP_BASE}/v3/weather/weatherInfo"
URL_GEOCODE = f"{AMAP_BASE}/v3/geocode/geo"
URL_REGEO = f"{AMAP_BASE}/v3/geocode/regeo"
URL_DRIVING = f"{AMAP_BASE}/v3/direction/driving"
URL_TRANSIT = f"{AMAP_BASE}/v3/direction/transit/integrated"
URL_WALKING = f"{AMAP_BASE}/v3/direction/walking"

REQUEST_TIMEOUT = 8.0
CACHE_TTL = 600.0  # seconds

# Amap returns HTTP 200 for domain errors; `status == "1"` is the only success.
_AMAP_OK = "1"

# --- Rate limiting -----------------------------------------------------------
# A personal Amap key is capped at a few QPS per endpoint. Stage 2 fires up to
# eight calls for a single four-waypoint route (4 × regeo + N × weather), which
# reliably trips `CUQPS_HAS_EXCEEDED_THE_LIMIT`. Two defences:
#   1. a process-wide minimum interval between outbound calls, and
#   2. bounded retries with exponential backoff for the throttle infocodes.
_MIN_INTERVAL_S = 0.4
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = (0.6, 1.2, 2.4)
_RETRY_INFOCDES = {"10004", "10021", "10029"}  # too frequent / QPS exceeded

_cache: dict[str, tuple[float, Any]] = {}
_rate_lock = asyncio.Lock()
_last_call_at = 0.0


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


async def _throttle() -> None:
    """Serialise outbound calls so we stay under the key's per-second ceiling."""
    global _last_call_at
    async with _rate_lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = time.monotonic()


def _is_throttled(payload: dict) -> bool:
    """True when Amap rejected the call purely because we were too fast."""
    if str(payload.get("status", "")).strip() == _AMAP_OK:
        return False
    return str(payload.get("infocode", "")).strip() in _RETRY_INFOCDES


async def _get_json(url: str, params: dict[str, str]) -> dict:
    """
    GET + JSON decode, with rate limiting and bounded retry.

    Raises `UpstreamError` for transport/parse failures. Domain failures are
    returned to the caller (who runs `_require_ok`) so it can decide whether the
    error is retryable — except throttling, which is retried here.
    """
    cache_key = url + "|" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    payload: dict | None = None
    for attempt in range(_MAX_RETRIES + 1):
        await _throttle()

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
            parsed = resp.json()
        except ValueError as exc:
            raise UpstreamError("BAD_RESPONSE", "高德返回内容不是合法 JSON") from exc

        if not isinstance(parsed, dict):
            raise UpstreamError("BAD_RESPONSE", "高德返回结构异常")

        payload = parsed
        if _is_throttled(parsed) and attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
            continue
        break

    assert payload is not None  # loop always assigns on the first iteration

    # Do not cache a throttled/failed payload: the next call should retry fresh.
    if str(payload.get("status", "")).strip() == _AMAP_OK:
        _cache_put(cache_key, payload)
    return payload


def _require_ok(payload: dict, *, what: str) -> None:
    """Raise when Amap reports a domain-level failure."""
    status = str(payload.get("status", "")).strip()
    if status == _AMAP_OK:
        return
    info = str(payload.get("info", "UNKNOWN") or "UNKNOWN")
    infocode = str(payload.get("infocode", "") or "")
    if infocode in _RETRY_INFOCDES:
        # Retries in `_get_json` already backed off; tell the user something
        # actionable instead of surfacing the raw provider code.
        raise UpstreamError(
            "RATE_LIMITED",
            f"高德{what}接口限流（请求过于频繁），请稍后重试",
        )
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
    step_spans: list[dict] = []
    for step in path.get("steps") or []:
        polyline.extend(_decode_polyline(step.get("polyline", "")))
        # Stage 3 needs per-step time to place an arrival clock on each
        # waypoint: congestion is not uniform, so total-duration × ratio would
        # put the 2/3 point in the wrong hour on a jammed ring road.
        step_spans.append(
            {
                "distance_m": _to_float(step.get("distance")) or 0.0,
                "duration_s": _to_float(step.get("duration")) or 0.0,
            }
        )

    return {
        "origin": origin,
        "destination": destination,
        "distance_m": _to_float(path.get("distance")) or 0.0,
        "duration_s": _to_float(path.get("duration")) or 0.0,
        "toll_m": _to_float(path.get("tolls")) or 0.0,
        "traffic_lights": int(_to_float(path.get("traffic_lights")) or 0),
        "step_count": len(path.get("steps") or []),
        "step_spans": step_spans,
        "polyline": polyline,
        "source": "amap",
    }


# ========== 逆地理编码（阶段二：采样点 → 区县） ==========
async def regeo(location: str) -> dict:
    """
    `"lon,lat"` → administrative area info via `/v3/geocode/regeo`.

    Used by the route pipeline: every sampled waypoint is turned into a
    district + adcode, and the adcode is what we feed to the weather endpoint
    (district-level is more precise than city-level for a highway waypoint).

    Note: for 直辖市 the response often has an empty `city` but a populated
    `district`, so callers should prefer `district` and only fall back to
    `city`/`province`.
    """
    location = (location or "").strip()
    if "," not in location:
        raise UpstreamError("BAD_LOCATION", "逆地理编码需要「经度,纬度」格式的坐标")

    key = amap_route_key()
    if not key:
        raise UpstreamError("MISSING_KEY", "未配置 AMAP_ROUTE_API_KEY")

    payload = await _get_json(
        URL_REGEO,
        {"key": key, "location": location, "extensions": "base", "output": "JSON"},
    )
    _require_ok(payload, what="逆地理编码")

    result = payload.get("regeocode") or {}
    component = result.get("addressComponent") or {}

    def _text(value: Any) -> str:
        """Amap returns `[]` for empty string fields, which is not a string."""
        return value if isinstance(value, str) else ""

    lon = lat = None
    if "," in location:
        lon_str, lat_str = location.split(",", 1)
        lon, lat = _to_float(lon_str), _to_float(lat_str)

    return {
        "location": location,
        "longitude": lon,
        "latitude": lat,
        "formatted": _text(result.get("formatted_address")),
        "province": _text(component.get("province")),
        "city": _text(component.get("city")),
        "district": _text(component.get("district")),
        "township": _text(component.get("township")),
        "adcode": _text(component.get("adcode")),
        "source": "amap",
    }


# ========== 公交 / 步行路径（阶段二） ==========
def _polyline_from_steps(steps: list[dict]) -> list[tuple[float, float]]:
    """Flatten `steps[].polyline` blocks into one coordinate list."""
    points: list[tuple[float, float]] = []
    for step in steps or []:
        points.extend(_decode_polyline(step.get("polyline", "")))
    return points


async def route_transit(origin: str, destination: str, *, city: str) -> dict:
    """
    Public-transport route via `/v3/direction/transit/integrated`.

    `city` (the origin city) is mandatory for this endpoint — Amap uses it to
    decide which bus network to search. Cross-city journeys additionally need
    `cityd`; when Amap rejects the pair we surface `TRANSIT_CROSS_CITY` so the
    caller can tell the user to try driving instead of silently swapping modes.
    """
    key = amap_route_key()
    if not key:
        raise UpstreamError("MISSING_KEY", "未配置 AMAP_ROUTE_API_KEY")

    city = (city or "").strip()
    if not city:
        raise UpstreamError(
            "MISSING_CITY",
            "公交路径规划需要起点城市信息，无法从起点推断城市",
        )

    payload = await _get_json(
        URL_TRANSIT,
        {
            "key": key,
            "origin": origin,
            "destination": destination,
            "city": city,
            "cityd": city,
            "strategy": "0",
            "nightflag": "0",
            "extensions": "base",
            "output": "JSON",
        },
    )

    # Cross-city transit answers with status=0 and a variety of infocodes; map
    # the whole class onto one actionable code instead of leaking provider text.
    if str(payload.get("status", "")).strip() != _AMAP_OK:
        info = str(payload.get("info", "") or "")
        infocode = str(payload.get("infocode", "") or "")
        raise UpstreamError(
            "TRANSIT_CROSS_CITY",
            "该公交路线无法规划（高德公交接口仅支持同城，跨城请改用驾车）：" + (info or infocode or "未知原因"),
        )

    route = payload.get("route") or {}
    transits = route.get("transits") or []
    if not transits:
        raise UpstreamError("EMPTY_ROUTE", "高德未返回可用公交方案")

    best = transits[0]
    polyline: list[tuple[float, float]] = []
    transfer_count = 0
    for segment in best.get("segments") or []:
        walking = segment.get("walking") or {}
        polyline.extend(_polyline_from_steps(walking.get("steps") or []))
        bus = segment.get("bus") or {}
        for line in bus.get("buslines") or []:
            polyline.extend(_decode_polyline(line.get("polyline", "")))
            transfer_count += 1
        railway = segment.get("railway") or {}
        if railway.get("polyline"):
            polyline.extend(_decode_polyline(railway.get("polyline", "")))
        taxi = segment.get("taxi") or {}
        if taxi.get("polyline"):
            polyline.extend(_decode_polyline(taxi.get("polyline", "")))

    return {
        "origin": origin,
        "destination": destination,
        "mode": "transit",
        "distance_m": _to_float(route.get("distance")) or 0.0,
        "duration_s": _to_float(best.get("duration")) or 0.0,
        "cost_yuan": _to_float(best.get("cost")) or 0.0,
        "transfer_count": max(0, transfer_count - 1),
        "walking_distance_m": _to_float(best.get("walking_distance")) or 0.0,
        "step_count": transfer_count,
        "polyline": polyline,
        "source": "amap",
    }


async def route_walking(origin: str, destination: str) -> dict:
    """Walking route via `/v3/direction/walking` (Amap caps this at ~100 km)."""
    key = amap_route_key()
    if not key:
        raise UpstreamError("MISSING_KEY", "未配置 AMAP_ROUTE_API_KEY")

    payload = await _get_json(
        URL_WALKING,
        {
            "key": key,
            "origin": origin,
            "destination": destination,
            "output": "JSON",
        },
    )
    _require_ok(payload, what="步行路径规划")

    route = payload.get("route") or {}
    paths = route.get("paths") or []
    if not paths:
        raise UpstreamError("EMPTY_ROUTE", "高德未返回可用步行路径")

    path = paths[0]
    return {
        "origin": origin,
        "destination": destination,
        "mode": "walking",
        "distance_m": _to_float(path.get("distance")) or 0.0,
        "duration_s": _to_float(path.get("duration")) or 0.0,
        "step_count": len(path.get("steps") or []),
        "polyline": _polyline_from_steps(path.get("steps") or []),
        "source": "amap",
    }


async def route_by_mode(origin: str, destination: str, *, mode: str, city: str = "") -> dict:
    """Dispatch to the right routing endpoint. `mode` is already validated."""
    if mode == "transit":
        return await route_transit(origin, destination, city=city)
    if mode == "walking":
        return await route_walking(origin, destination)
    return await route_driving(origin, destination)


# ========== 里程等分采样（阶段二） ==========
def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two `(lon, lat)` pairs."""
    lon1, lat1 = a
    lon2, lat2 = b
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(h)))


def _cumulative_distances(polyline: list[tuple[float, float]]) -> list[float]:
    """Running distance from the first point, aligned 1:1 with `polyline`."""
    cumulative = [0.0]
    for prev, cur in zip(polyline, polyline[1:]):
        cumulative.append(cumulative[-1] + _haversine_m(prev, cur))
    return cumulative


def _point_at_distance(
    polyline: list[tuple[float, float]],
    cumulative: list[float],
    target_m: float,
) -> tuple[float, float]:
    """Linear interpolation of the coordinate `target_m` metres along the path."""
    if target_m <= 0:
        return polyline[0]
    if target_m >= cumulative[-1]:
        return polyline[-1]

    # `bisect` would work too, but the lists here are short and this keeps the
    # module dependency-free.
    for i in range(1, len(cumulative)):
        if cumulative[i] >= target_m:
            span = cumulative[i] - cumulative[i - 1]
            if span <= 0:
                return polyline[i]
            ratio = (target_m - cumulative[i - 1]) / span
            lon1, lat1 = polyline[i - 1]
            lon2, lat2 = polyline[i]
            return (lon1 + (lon2 - lon1) * ratio, lat1 + (lat2 - lat1) * ratio)
    return polyline[-1]


def sample_route_points(route: dict, n: int) -> list[tuple[float, float]]:
    """
    Sample `n` waypoints (endpoints included) **by travelled distance**.

    Why distance and not polyline index: a decoded polyline has a vertex every
    ~50–200 m on curves but can run kilometres straight on a motorway, so
    index-based sampling bunches waypoints where the road bends and skips whole
    stretches of highway. Splitting the cumulative length into `n-1` equal
    parts gives true 0 / ⅓ / ⅔ / 1 positions.

    Returns exactly `n` points (duplicates possible only for a degenerate
    zero-length path). Pure function — no network, unit-testable.
    """
    polyline: list[tuple[float, float]] = route.get("polyline") or []
    if not polyline:
        return []
    n = max(1, int(n))
    if n == 1 or len(polyline) == 1:
        return [polyline[0]] * n

    cumulative = _cumulative_distances(polyline)
    total = cumulative[-1]
    if total <= 0:
        return [polyline[0]] * n

    return [
        _point_at_distance(polyline, cumulative, total * i / (n - 1))
        for i in range(n)
    ]
