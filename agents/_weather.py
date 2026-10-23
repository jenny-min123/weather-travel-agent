"""
实况天气编排层 — 决定「这一次查询到底能拿到多细的天气」。

设计目标是**保住区县精度**，而不是「能用就行」。取数顺序：

    ① 和风 `/v7/weather/now`（按区县中心坐标）   → level=district, provider=qweather
    ② 高德 `/v3/weather/weatherInfo`（按 adcode） → level=district, provider=amap
    ③ 高德 `/v3/weather/weatherInfo`（按地级市）  → level=city,     provider=amap
    ④ 全挂 → 抛最后的 UpstreamError，由工具层转成 ok=false

为什么要有第 ② 层：高德本来就能返回区级数据（`city=440305` → 南山区），
只是它**不认「深圳南山区」这种复合串**（返回 status=1 但 lives=[]）。
既然 `_place` 已经拿到了 adcode，就顺手用上，没必要直接掉到市级。

`resolution` 块是给模型看的唯一契约，前端角标也读它：

    level / provider      决定角标文案（区级 · 和风 / 市级 · 高德）
    degraded              只有「本来要区级、最后只拿到市级」才为 true
    requestedLevel        用户原本要的精度（裸区名/含「区县」后缀 → district）
    displayName           省+市+区全称，裸区名有歧义时靠它播报
"""

from __future__ import annotations

from ._amap import UpstreamError, weather_live
from ._config import district_weather_enabled
from ._place import looks_like_district, resolve_place
from ._qweather import now_to_live, weather_now


def _coords_param(place: dict) -> str:
    """District centroid as QWeather's `"lon,lat"` (two decimals max)."""
    lon, lat = place.get("lon"), place.get("lat")
    if lon is None or lat is None:
        return ""
    return f"{lon:.2f},{lat:.2f}"


def _resolved_location(place: dict) -> dict:
    """Same shape stage 4 already puts on `live.resolvedLocation`."""
    return {
        "longitude": place.get("lon"),
        "latitude": place.get("lat"),
        "adcode": place.get("adcode", ""),
        "province": place.get("province", ""),
        "city": place.get("city", ""),
        "district": place.get("district", ""),
        "township": "",
        "formatted": place.get("displayName", ""),
        "resolvedBy": place.get("resolvedBy", ""),
    }


def _resolution(place: dict, *, level: str, provider: str, attempts: list[dict]) -> dict:
    requested = "district" if (place.get("level") == "district" or looks_like_district(place.get("query", ""))) else "city"
    degraded = requested == "district" and level == "city"
    return {
        "level": level,
        "requestedLevel": requested,
        "provider": provider,
        "displayName": place.get("displayName", ""),
        "province": place.get("province", ""),
        "city": place.get("city", ""),
        "district": place.get("district", ""),
        "adcode": place.get("adcode", ""),
        "ambiguous": bool(place.get("ambiguous")),
        "degraded": degraded,
        "degradedReason": "district_unavailable" if degraded else "",
        "attempts": attempts,
    }


async def current_weather(query: str) -> dict:
    """
    Resolve `query` and fetch the finest available live conditions.

    Returns `{"current": <amap-shaped live>, "resolution": {...}}`.
    Raises `UpstreamError` only when every layer failed — including an
    unresolvable place name.
    """
    place = await resolve_place(query)
    attempts: list[dict] = []
    want_district = place.get("level") == "district"

    # ---------- ① 和风区级 ----------
    if want_district and district_weather_enabled():
        point = _coords_param(place)
        if not point:
            attempts.append({"provider": "qweather", "level": "district", "ok": False, "error": "NO_COORDS"})
        else:
            result = await weather_now(point)
            if result.get("available"):
                live = now_to_live(
                    result,
                    province=place.get("province", ""),
                    city=place.get("city", ""),
                    district=place.get("district", ""),
                    adcode=place.get("adcode", ""),
                )
                live["resolvedLocation"] = _resolved_location(place)
                attempts.append({"provider": "qweather", "level": "district", "ok": True})
                return {
                    "current": live,
                    "resolution": _resolution(place, level="district", provider="qweather", attempts=attempts),
                }
            attempts.append(
                {
                    "provider": "qweather",
                    "level": "district",
                    "ok": False,
                    "error": str(result.get("reason", "UNAVAILABLE"))[:80],
                }
            )

    # ---------- ② 高德区级（adcode） ----------
    if want_district and place.get("adcode"):
        try:
            live = await weather_live(place["adcode"])
        except UpstreamError as exc:
            attempts.append({"provider": "amap", "level": "district", "ok": False, "error": exc.code})
        else:
            if str(live.get("adcode", "")).strip() == str(place["adcode"]).strip():
                live["resolvedLocation"] = _resolved_location(place)
                attempts.append({"provider": "amap", "level": "district", "ok": True})
                return {
                    "current": live,
                    "resolution": _resolution(place, level="district", provider="amap", attempts=attempts),
                }
            attempts.append(
                {
                    "provider": "amap",
                    "level": "district",
                    "ok": False,
                    "error": f"ADCODE_MISMATCH {live.get('adcode')}",
                }
            )

    # ---------- ③ 高德市级 ----------
    city_query = place.get("city") or place.get("province") or place.get("query", "")
    try:
        live = await weather_live(city_query)
    except UpstreamError as exc:
        attempts.append({"provider": "amap", "level": "city", "ok": False, "error": exc.code})
        raise UpstreamError(
            exc.code,
            f"「{place.get('displayName') or query}」的实况天气查询失败：{exc.message}",
        ) from exc

    live["resolvedLocation"] = _resolved_location(place)
    attempts.append({"provider": "amap", "level": "city", "ok": True})
    return {
        "current": live,
        "resolution": _resolution(place, level="city", provider="amap", attempts=attempts),
    }
