"""
Route-weather orchestration — private module (starts with `_`), no route.

Stage 2 stitches four Amap surfaces into one answer:

    起点/终点文本 ──geocode──▶ 经纬度
                            │
                            ├─direction(driving|transit|walking)─▶ 轨迹折线
                            │                                     │
                            │                        按里程等分采样（含首尾）
                            │                                     │
                            └────────────regeo────────────────────▶ 区县 + adcode
                                                                  │
                                                   weatherInfo(adcode)
                                                                  │
                                                     build_advice()（规则引擎）
                                                                  │
                                                          逐点建议 + 沿途预警

Design notes:
  * Routing/geocoding use `AMAP_ROUTE_API_KEY`; weather uses
    `AMAP_WEATHER_API_KEY`. Both fall back to each other inside `_config`.
  * Upstream failures are *not* swallowed here — `UpstreamError` propagates and
    the tool layer turns it into `{ok: false}`. The one exception is a single
    bad waypoint: one unreachable district must not void the whole answer, so
    that point is marked `error` and the rest still render.
  * Concurrency is capped (`_MAX_CONCURRENCY`) because a personal Amap key has
    a low QPS ceiling and four waypoints already mean up to eight calls.
  * Weather is fetched per **distinct adcode**, so a route that stays inside
    one district costs exactly one weather call.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ._advice import build_advice
from ._amap import (
    UpstreamError,
    _cumulative_distances,
    geocode,
    regeo,
    route_by_mode,
    sample_route_points,
    weather_live,
)
from ._config import default_city, route_sample_points, route_weather_enabled

SUPPORTED_MODES = ("driving", "transit", "walking")
_MODE_ZH = {"driving": "驾车", "transit": "公交", "walking": "步行"}
_MAX_CONCURRENCY = 3

_RAIN_MARKERS = ("雨", "雪", "冰粒", "冰雹")

# Levels Amap falls back to when it has no real match for a place name: rather
# than returning nothing, it hands back *some* administrative area. For a
# landmark-style query ("首都机场T3") that is never right, so we treat it as a
# failure instead of quietly reporting the weather of an unrelated township.
_LOW_CONFIDENCE_LEVELS = {"乡镇", "村庄", "未知"}
# ...unless the query itself names an administrative unit or a road, in which
# case a township/village hit is a legitimate answer.
_ADMIN_HINTS = ("镇", "乡", "村", "街道", "路", "街", "大道", "巷", "胡同")
# Boilerplate that appears in almost every Amap address and therefore carries no
# signal when checking whether a match is plausible.
_GENERIC_CHARS = set("省市区县镇乡村街道号的路")


# ========== 端点解析 ==========
def _as_lonlat(text: str) -> tuple[float, float] | None:
    """Parse a literal `"lon,lat"` argument, or return None if it is a name."""
    parts = (text or "").split(",")
    if len(parts) != 2:
        return None
    try:
        lon, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def _significant(text: str) -> str:
    """CJK characters of `text` minus administrative boilerplate."""
    return "".join(
        ch for ch in text
        if "\u4e00" <= ch <= "\u9fff" and ch not in _GENERIC_CHARS
    )


def _shares_bigram(query: str, formatted: str) -> bool:
    """True when a 2-gram of the query survives into the matched address."""
    q = _significant(query)
    f = _significant(formatted)
    if not q:
        return True  # nothing to compare — do not block on a non-CJK query
    if len(q) < 2:
        return q in f
    return any(q[i : i + 2] in f for i in range(len(q) - 1))


def _is_low_confidence(query: str, geo: dict) -> bool:
    """
    Detect Amap's "best effort" fallback for a name it could not really find.

    Only township/village-level hits are suspect, and only when the query did
    not ask for one ("XX镇" is a fine query). The discriminator is character
    overlap: "首都机场T3" → "首都机场街道" shares 首都机场 and is genuine, while
    "不存在的城市ZZZ" → "泰州市…城市街道" shares nothing meaningful and is a
    fallback we refuse to report weather for.
    """
    if geo.get("level") not in _LOW_CONFIDENCE_LEVELS:
        return False
    if any(hint in query for hint in _ADMIN_HINTS):
        return False
    return not _shares_bigram(query, geo.get("formatted") or "")


async def resolve_endpoint(text: str, *, allow_default: bool) -> dict:
    """
    Turn a free-text place (or `"lon,lat"`) into a routing endpoint.

    Empty input is only legal for the origin: the user's "from here" falls back
    to `DEFAULT_CITY`, and `assumedDefaultCity` records that substitution so the
    final answer can be honest about it.
    """
    raw = (text or "").strip()
    assumed = False
    if not raw:
        if not allow_default:
            raise UpstreamError("MISSING_DESTINATION", "未提供终点，无法规划路线")
        raw = default_city()
        assumed = True

    literal = _as_lonlat(raw)
    if literal:
        return {
            "query": raw,
            "longitude": literal[0],
            "latitude": literal[1],
            "city": "",
            "province": "",
            "adcode": "",
            "assumedDefaultCity": assumed,
        }

    geo = await geocode(raw)
    if geo.get("longitude") is None or geo.get("latitude") is None:
        raise UpstreamError("EMPTY_GEOCODE", f"无法解析地名「{raw}」的坐标")
    if _is_low_confidence(raw, geo):
        raise UpstreamError(
            "LOW_CONFIDENCE_GEOCODE",
            f"高德无法准确识别地名「{raw}」（模糊匹配到「{geo.get('formatted', '')}」），请补充所在城市或更具体的地址",
        )

    return {
        "query": raw,
        "longitude": geo["longitude"],
        "latitude": geo["latitude"],
        "city": geo.get("city", ""),
        "province": geo.get("province", ""),
        "adcode": geo.get("adcode", ""),
        "assumedDefaultCity": assumed,
    }


def _coord(endpoint: dict) -> str:
    return f"{endpoint['longitude']:.6f},{endpoint['latitude']:.6f}"


# ========== 单个采样点 ==========
def _role(index: int, total: int, ratio: float) -> str:
    if index == 0:
        return "起点"
    if index == total - 1:
        return "终点"
    return f"{index}/{total - 1} 处"


def _brief_advice(advice: dict | None) -> dict | None:
    """Trim `build_advice()` down to what a per-waypoint line needs."""
    if not advice:
        return None
    clothing = advice.get("clothing") or {}
    umbrella = advice.get("umbrella") or {}
    sport = advice.get("outdoorSport") or {}
    wind = advice.get("wind") or {}
    return {
        "clothing": {"level": clothing.get("level", ""), "text": clothing.get("text", "")},
        "umbrella": {
            "needed": umbrella.get("needed"),
            "level": umbrella.get("level", ""),
            "reason": umbrella.get("reason", ""),
        },
        "outdoorSport": {"advised": sport.get("advised"), "reason": sport.get("reason", "")},
        "wind": {"text": wind.get("text", "")},
    }


async def _sample_one(
    point: tuple[float, float],
    index: int,
    total: int,
    ratio: float,
    total_distance_m: float,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Reverse-geocode one waypoint. Weather is attached later, by adcode."""
    location = f"{point[0]:.6f},{point[1]:.6f}"
    entry: dict[str, Any] = {
        "index": index,
        "ratio": round(ratio, 4),
        "role": _role(index, total, ratio),
        "distanceFromStartKm": round(total_distance_m * ratio / 1000.0, 1),
        "longitude": round(point[0], 6),
        "latitude": round(point[1], 6),
        "formatted": "",
        "province": "",
        "city": "",
        "district": "",
        "township": "",
        "adcode": "",
        "districtSource": "none",
        "weather": None,
        "advice": None,
        "error": None,
    }

    try:
        async with semaphore:
            area = await regeo(location)
    except UpstreamError as exc:
        entry["error"] = f"逆地理编码失败：{exc.message}"
        return entry

    entry.update(
        {
            "formatted": area.get("formatted", ""),
            "province": area.get("province", ""),
            "city": area.get("city", ""),
            "district": area.get("district", ""),
            "township": area.get("township", ""),
            "adcode": area.get("adcode", ""),
        }
    )

    # Prefer district, then city, then province — 直辖市 often have no `city`.
    for field in ("district", "city", "province"):
        if entry[field]:
            entry["districtSource"] = field
            break
    return entry


# ========== 逐点天气 ==========
async def _weather_by_adcode(
    samples: list[dict], semaphore: asyncio.Semaphore
) -> tuple[dict[str, dict | None], dict[str, str]]:
    """
    Fetch live weather once per distinct adcode.

    Returns `(adcode → weather, adcode → error)`; a failed area is remembered
    so we do not retry the same dead adcode four times.
    """
    codes = sorted({s["adcode"] for s in samples if s.get("adcode")})
    weather: dict[str, dict | None] = {}
    errors: dict[str, str] = {}

    async def _one(code: str) -> tuple[str, dict | None, str | None]:
        try:
            async with semaphore:
                return code, await weather_live(code), None
        except UpstreamError as exc:
            return code, None, exc.message

    for code, data, err in await asyncio.gather(*(_one(c) for c in codes)):
        weather[code] = data
        if err:
            errors[code] = err
    return weather, errors


# ========== 汇总 ==========
def _build_alerts(samples: list[dict], route: dict) -> list[str]:
    alerts: list[str] = []
    with_weather = [s for s in samples if s.get("weather")]

    temps = [s["weather"]["temperature"] for s in with_weather if s["weather"].get("temperature") is not None]
    if len(temps) >= 2 and max(temps) - min(temps) >= 5:
        alerts.append(f"沿途温差 {max(temps) - min(temps):.0f}℃（{min(temps):.0f}–{max(temps):.0f}℃），注意增减衣物")

    wet = [
        s for s in with_weather
        if any(marker in str(s["weather"].get("weather", "")) for marker in _RAIN_MARKERS)
    ]
    if wet:
        where = "、".join(f'{s["role"]}（{s.get("district") or s.get("city")}）' for s in wet)
        alerts.append(f"{len(wet)} 个采样点有降水：{where}")

    windy = [
        s for s in with_weather
        if _wind_scale(s["weather"].get("windpower", "")) >= 6
    ]
    if windy:
        where = "、".join(s["role"] for s in windy)
        alerts.append(f"沿途风力较大（≥6 级）：{where}，驾车注意侧风")

    if route.get("mode") == "transit" and route.get("transfer_count"):
        alerts.append(f'公交方案需换乘 {route["transfer_count"]} 次，全程步行 {route.get("walking_distance_m", 0) / 1000:.1f} km')

    codes = {s["adcode"] for s in samples if s.get("adcode")}
    if len(codes) == 1 and len(samples) > 1:
        alerts.append("整条路线集中在同一区县，沿途天气差异很小")

    return alerts


def _wind_scale(windpower: Any) -> int:
    """`"≤3"`, `"3-4"`, `"6"` → highest numeric level mentioned (0 if none)."""
    text = str(windpower or "")
    digits = [int(part) for part in re.findall(r"\d+", text)]
    return max(digits) if digits else 0


def _build_summary(
    samples: list[dict],
    route: dict,
    mode: str,
    origin: dict,
    destination: dict,
) -> str:
    parts: list[str] = []
    distance_km = (route.get("distance_m") or 0) / 1000.0
    duration_min = (route.get("duration_s") or 0) / 60.0

    head = f"{_MODE_ZH.get(mode, mode)}全程 {distance_km:.1f} km"
    if duration_min >= 1:
        head += f"，约 {duration_min:.0f} 分钟"
    parts.append(head + "。")

    endpoints = [s for s in samples if s["role"] in ("起点", "终点")]
    for sample in endpoints:
        weather = sample.get("weather") or {}
        place = sample.get("district") or sample.get("city") or sample.get("province") or "未知区域"
        if weather:
            temp = weather.get("temperature")
            temp_text = f" {temp:.0f}℃" if temp is not None else ""
            parts.append(f'{sample["role"]}（{place}）{weather.get("weather", "")}{temp_text}；')
        elif sample.get("error"):
            parts.append(f'{sample["role"]}（{place}）天气暂缺；')

    if origin.get("assumedDefaultCity"):
        parts.append(f'起点未指定，按默认城市「{origin["query"]}」计算。')
    if destination.get("assumedDefaultCity"):
        parts.append(f'终点未指定，按默认城市「{destination["query"]}」计算。')

    return "".join(parts).strip()


# ========== 入口 ==========
async def route_weather(origin: str, destination: str, mode: str = "driving") -> dict:
    """
    Plan a route and describe the weather at 0 / ⅓ / ⅔ / 1 of its length.

    Raises `UpstreamError` for anything that makes the whole answer meaningless
    (feature disabled, unknown place, unroutable pair). Individual waypoints may
    still carry their own `error`.
    """
    if not route_weather_enabled():
        raise UpstreamError(
            "ROUTE_WEATHER_DISABLED",
            "沿途天气功能未开启（ROUTE_WEATHER_ENABLED=false）",
        )

    mode_clean = (mode or "driving").strip().lower()
    if mode_clean not in SUPPORTED_MODES:
        raise UpstreamError(
            "BAD_MODE",
            f"不支持的出行方式「{mode}」，可选：driving / transit / walking",
        )

    origin_ep = await resolve_endpoint(origin, allow_default=True)
    destination_ep = await resolve_endpoint(destination, allow_default=False)

    # Transit needs the origin city; geocode gives us "北京市" or "" for 直辖市.
    origin_city = origin_ep.get("city") or origin_ep.get("province") or origin_ep.get("adcode") or ""

    route = await route_by_mode(
        _coord(origin_ep),
        _coord(destination_ep),
        mode=mode_clean,
        city=origin_city,
    )

    polyline = route.get("polyline") or []
    if not polyline:
        raise UpstreamError("EMPTY_ROUTE", "路径规划未返回可用轨迹，无法沿途采样")

    count = route_sample_points()
    points = sample_route_points(route, count)
    total_distance_m = route.get("distance_m") or 0.0
    if total_distance_m <= 0:
        # Fall back to the geometric length when Amap omits `distance`.
        total_distance_m = _polyline_length(points)

    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    samples = await asyncio.gather(
        *(
            _sample_one(
                point,
                index,
                len(points),
                index / (len(points) - 1) if len(points) > 1 else 0.0,
                total_distance_m,
                semaphore,
            )
            for index, point in enumerate(points)
        )
    )
    samples = list(samples)

    weather_map, weather_errors = await _weather_by_adcode(samples, semaphore)
    for sample in samples:
        code = sample.get("adcode", "")
        if not code:
            if not sample.get("error"):
                sample["error"] = "该采样点未能解析出行政区，已跳过天气查询"
            continue
        data = weather_map.get(code)
        if data:
            sample["weather"] = data
            sample["advice"] = _brief_advice(build_advice(data, None))
        else:
            sample["error"] = f"天气查询失败：{weather_errors.get(code, '未知原因')}"

    covered = sum(1 for s in samples if s.get("weather"))
    degraded = None
    if covered < len(samples):
        degraded = {
            "reason": f"{len(samples) - covered} 个采样点未能取到天气，结果不完整",
            "points": [s["role"] for s in samples if not s.get("weather")],
        }

    return {
        "mode": mode_clean,
        "modeLabel": _MODE_ZH.get(mode_clean, mode_clean),
        "origin": {
            "query": origin_ep["query"],
            "resolved": origin_ep.get("city") or origin_ep.get("province") or "",
            "longitude": origin_ep["longitude"],
            "latitude": origin_ep["latitude"],
            "assumedDefaultCity": origin_ep.get("assumedDefaultCity", False),
        },
        "destination": {
            "query": destination_ep["query"],
            "resolved": destination_ep.get("city") or destination_ep.get("province") or "",
            "longitude": destination_ep["longitude"],
            "latitude": destination_ep["latitude"],
            "assumedDefaultCity": destination_ep.get("assumedDefaultCity", False),
        },
        "route": {
            "distanceKm": round((route.get("distance_m") or 0) / 1000.0, 1),
            "durationMin": round((route.get("duration_s") or 0) / 60.0),
            "trafficLights": route.get("traffic_lights", 0),
            "tollYuan": round((route.get("toll_m") or 0), 0),
            "transferCount": route.get("transfer_count", 0),
            "walkingDistanceKm": round((route.get("walking_distance_m") or 0) / 1000.0, 1),
        },
        "sampleCount": len(samples),
        "samples": samples,
        "alerts": _build_alerts(samples, {"mode": mode_clean, **route}),
        "summary": _build_summary(samples, route, mode_clean, origin_ep, destination_ep),
        "degraded": degraded,
        "source": "amap",
        "generatedBy": "amap + rule-engine",
    }


def _polyline_length(points: list[tuple[float, float]]) -> float:
    """Geometric length of a sampled path, used only when Amap omits distance."""
    if len(points) < 2:
        return 0.0
    return _cumulative_distances(points)[-1]
