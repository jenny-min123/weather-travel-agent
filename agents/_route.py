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

Stage 3 adds time alignment on top of that pipeline:

    出发时刻(+08:00) ──分段累计耗时──▶ 每点到达时刻 ──最近整点──▶ 24h/72h/168h/7d
                                                                      │
                                                    命中 → 该点用预报
                                                    未命中 → 该点回落实况

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
  * Stage 3 keeps every failure local to one waypoint: forecast lookup is
    wrapped per point, and only points that could *not* get a forecast pay for
    a live call. `ROUTE_FORECAST_ENABLED=false` restores stage 2 exactly.
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
from ._config import (
    default_city,
    route_forecast_enabled,
    route_forecast_tolerance_min,
    route_sample_points,
    route_weather_enabled,
)
from ._qweather import forecast_at
from ._timealign import (
    TZ_NAME,
    arrival_offsets,
    arrival_times,
    format_clock,
    format_iso,
    local_now,
    parse_fx_time,
)

SUPPORTED_MODES = ("driving", "transit", "walking")
_MODE_ZH = {"driving": "驾车", "transit": "公交", "walking": "步行"}
_MAX_CONCURRENCY = 3
#: QWeather's personal-key ceiling is lower than Amap's; keep it tighter so a
#: burst of four waypoints never looks like abuse.
_FORECAST_CONCURRENCY = 2

#: Shown verbatim to the user (and required in the answer by the instructions).
TIME_DISCLAIMER = "预估时间基于当前路况计算，实际到达时间可能因拥堵延后，请谨慎参考。"

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


# ========== 逐点预报（阶段三） ==========
async def _forecast_one(
    sample: dict,
    arrival,
    departure,
    semaphore: asyncio.Semaphore,
    tolerance_min: int,
) -> None:
    """
    Attach the stage-3 fields to one waypoint, in place.

    Deliberately defensive: a broken upstream, a bad `fxTime`, or anything else
    must only cost *this* point its forecast. The caller then falls back to live
    conditions for this point alone, and the other three still render.
    """
    sample["arrivalTime"] = format_iso(arrival)
    sample["arrivalOffsetMin"] = round(max((arrival - departure).total_seconds(), 0) / 60.0, 1)
    sample["forecastStatus"] = "unavailable"
    sample["forecastTier"] = ""
    sample["forecastTime"] = ""
    sample["forecastDeltaMin"] = None
    sample["forecastError"] = None

    try:
        # The waypoint's own coordinates, rounded to the two decimals QWeather
        # accepts — no extra city-lookup call, and the forecast is point-level.
        location = f"{sample['longitude']:.2f},{sample['latitude']:.2f}"
        outcome = await forecast_at(
            location,
            arrival,
            semaphore=semaphore,
            tolerance_min=tolerance_min,
        )
    except Exception as exc:  # noqa: BLE001 — isolation is the whole point
        sample["forecastStatus"] = "fallback_to_live"
        sample["forecastError"] = f"{type(exc).__name__}: {exc}"
        return

    sample["forecastStatus"] = outcome.get("status") or "unavailable"
    sample["forecastTier"] = outcome.get("tier") or ""
    sample["forecastTime"] = outcome.get("time") or ""
    sample["forecastDeltaMin"] = outcome.get("deltaMin")
    sample["forecastError"] = outcome.get("error")

    weather = outcome.get("weather")
    if weather:
        sample["weather"] = weather

    if sample["forecastStatus"] == "unavailable":
        # No tier covered this moment (or the chain was exhausted) — the caller
        # will try live conditions and relabel the point if that works.
        sample["forecastStatus"] = "fallback_to_live"


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

    # --- 阶段三：按到达时刻的预报预警 ---
    likely_wet = [
        s for s in samples
        if (s.get("weather") or {}).get("pop") is not None
        and float((s.get("weather") or {}).get("pop") or 0) >= 60
    ]
    if likely_wet:
        where = "、".join(
            f'{s["role"]}（{format_clock(parse_fx_time(s["forecastTime"])) if parse_fx_time(s.get("forecastTime")) else "到达时"} '
            f'降水概率 {float(s["weather"]["pop"]):.0f}%）'
            for s in likely_wet
        )
        alerts.append(f"按到达时刻预报，{where}")

    departures = [d for d in (parse_fx_time(s.get("arrivalTime")) for s in samples) if d]
    if departures and samples:
        first_day = departures[0].date()
        if any(moment.date() != first_day for moment in departures):
            alerts.append(f"行程跨天：预计 {format_clock(departures[-1])} 抵达，注意夜间行车安全")

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

        clock = ""
        moment = parse_fx_time(sample.get("arrivalTime"))
        if moment is not None:
            clock = f"，预计 {format_clock(moment)} 到达"

        # A live observation cannot answer "what will it be when I get there" —
        # say so at the exact place the number is quoted, not only in a footer.
        fallback_note = ""
        if sample.get("forecastStatus") == "fallback_to_live":
            fallback_note = "（超出预报范围，以下为当前实况）"

        if weather:
            temp = weather.get("temperature")
            temp_text = f" {temp:.0f}℃" if temp is not None else ""
            parts.append(
                f'{sample["role"]}（{place}）{weather.get("weather", "")}{temp_text}{clock}{fallback_note}；'
            )
        elif sample.get("error"):
            parts.append(f'{sample["role"]}（{place}）天气暂缺{clock}；')

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

    # ---------- 阶段三：为每个采样点算出到达时刻 ----------
    departure = local_now()
    forecast_enabled = route_forecast_enabled()
    tolerance_min = route_forecast_tolerance_min()
    offsets, basis = arrival_offsets(
        total_duration_s=route.get("duration_s"),
        total_distance_m=total_distance_m,
        ratios=[s["ratio"] for s in samples],
        step_spans=route.get("step_spans"),
    )
    arrivals = arrival_times(departure, offsets)

    if forecast_enabled:
        # Dedicated semaphore: QWeather's ceiling is tighter than Amap's, and
        # sharing one would let regeo calls starve the forecast calls.
        qweather_semaphore = asyncio.Semaphore(_FORECAST_CONCURRENCY)
        await asyncio.gather(
            *(
                _forecast_one(sample, arrivals[index], departure, qweather_semaphore, tolerance_min)
                for index, sample in enumerate(samples)
            )
        )
    else:
        # Kill switch: keep the useful arrival clock, skip every forecast call.
        for index, sample in enumerate(samples):
            sample["arrivalTime"] = format_iso(arrivals[index])
            sample["arrivalOffsetMin"] = round(offsets[index] / 60.0, 1)
            sample["forecastStatus"] = "disabled"
            sample["forecastTier"] = ""
            sample["forecastTime"] = ""
            sample["forecastDeltaMin"] = None
            sample["forecastError"] = None

    # ---------- 实况兜底：只为没拿到预报的点付请求 ----------
    pending = [sample for sample in samples if not sample.get("weather")]
    weather_map, weather_errors = await _weather_by_adcode(pending, semaphore)

    for sample in samples:
        if sample.get("weather"):
            sample["weatherBasis"] = "forecast"
            sample["advice"] = _brief_advice(build_advice(sample["weather"], None))
            continue

        code = sample.get("adcode", "")
        if not code:
            if forecast_enabled:
                sample["forecastStatus"] = "unavailable"
            if not sample.get("error"):
                sample["error"] = "该采样点未能解析出行政区，已跳过天气查询"
            continue

        data = weather_map.get(code)
        if data:
            sample["weather"] = data
            sample["weatherBasis"] = "live"
            sample["advice"] = _brief_advice(build_advice(data, None))
            if forecast_enabled:
                sample["forecastStatus"] = "fallback_to_live"
                if not sample.get("forecastError"):
                    sample["forecastError"] = "预报未覆盖该时刻，已改用当前实况"
        else:
            if forecast_enabled:
                sample["forecastStatus"] = "unavailable"
            sample["error"] = f"天气查询失败：{weather_errors.get(code, '未知原因')}"

    covered = sum(1 for s in samples if s.get("weather"))
    missing_points = [s["role"] for s in samples if not s.get("weather")]
    fallback_points = [s["role"] for s in samples if s.get("forecastStatus") == "fallback_to_live"]
    aligned_points = sum(
        1 for s in samples if s.get("forecastStatus") in ("hourly", "daily_fallback")
    )

    degraded = None
    reasons: list[str] = []
    if missing_points:
        reasons.append(f"{len(missing_points)} 个采样点未能取到天气")
    if fallback_points:
        reasons.append(f"{len(fallback_points)} 个采样点未能按到达时刻取到预报，已改报当前实况")
    if reasons:
        degraded = {
            "reason": "，".join(reasons),
            "points": missing_points,
            "timeAlignment": bool(fallback_points),
            "fallbackPoints": fallback_points,
        }

    summary = _build_summary(samples, route, mode_clean, origin_ep, destination_ep)
    if forecast_enabled:
        summary = f"{summary}{TIME_DISCLAIMER}"

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
        "summary": summary,
        "timeAlignment": {
            "enabled": forecast_enabled,
            "timezone": TZ_NAME,
            "departureAt": format_iso(departure),
            "arrivalAt": format_iso(arrivals[-1]) if arrivals else "",
            "basis": basis,
            "toleranceMin": tolerance_min,
            "alignedPoints": aligned_points,
            "fallbackPoints": len(fallback_points),
        },
        "timeDisclaimer": TIME_DISCLAIMER,
        "degraded": degraded,
        "source": "amap",
        "generatedBy": "amap + qweather + rule-engine",
    }


def _polyline_length(points: list[tuple[float, float]]) -> float:
    """Geometric length of a sampled path, used only when Amap omits distance."""
    if len(points) < 2:
        return 0.0
    return _cumulative_distances(points)[-1]
