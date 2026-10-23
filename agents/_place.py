"""
地名归一 — 把「用户随口说的地名 / 浏览器坐标」统一成可查询的行政区。

Why this module exists (阶段五，全部结论来自实测):

  * 高德 `/v3/weather/weatherInfo` **只认单一行政区名或 adcode**。
    传复合串「深圳南山区」「深圳市南山区」时返回 `status=1, info=OK` 但
    `count=0, lives=[]` —— 静默空数组，看起来像"没有这个城市"。传 adcode
    `440305` 或单说「南山区」才拿得到区级数据。
  * 裸区名是**有歧义的**：高德 `city=南山区` 会同时返回广东深圳南山(440305)
    和黑龙江鹤岗南山(230404)，`lives[0]` 取哪条全看返回顺序。
  * 和风 `/geo/v2/city/lookup?location=南山区` 更是首条命中黑龙江鹤岗
    (101051206)，深圳南山 (101280604) 排第二。

所以「取第一条」在这条链路上是 bug，不是实现细节。这里统一用
**Amap geocode → adcode + 区县中心坐标** 锁定唯一行政区，下游只认 adcode 和坐标。

返回结构（`Place`）：

    {
      "query": 用户原话,
      "province"/"city"/"district": 行政区名（可能为空串）,
      "adcode": "440305",           # 没有则空串
      "lon"/"lat": float | None,    # GCJ-02 区县中心点
      "level": "district" | "city", # 精度档位，决定要不要播报降级
      "displayName": "广东省深圳市南山区",
      "ambiguous": bool,            # 裸区名命中多个同名区时为 True
      "resolvedBy": "amap-geocode" | "amap-regeo" | "user-coords",
    }

`displayName` 必须带省市：裸区名有歧义时，模型照读全称，用户自己就能发现
"哦这不是我要的南山区"，比反问一句更不打断人。
"""

from __future__ import annotations

import time
from typing import Any

from ._amap import UpstreamError, geocode_all, regeo
from ._geo import format_coords, parse_coords

CACHE_TTL = 600.0

#: 行政区名后缀。注意「新区」「开发区」也以「区」结尾，用前缀判断即可。
DISTRICT_SUFFIXES = ("区", "县", "旗")
#: 地级/县级市、自治州、盟。用于把「深圳南山区」拆成「深圳市」+「南山区」。
CITY_SUFFIXES = ("市", "州", "盟", "地区")

_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str) -> dict | None:
    hit = _cache.get(key)
    if not hit:
        return None
    value, expires_at = hit
    if time.time() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: dict) -> None:
    _cache[key] = (value, time.time() + CACHE_TTL)


def looks_like_district(text: str) -> bool:
    """True when the string ends in a district-level suffix (区/县/旗)."""
    return bool(text) and text.strip().endswith(DISTRICT_SUFFIXES)


def split_city_district(text: str) -> tuple[str, str]:
    """
    `"深圳南山区"` → `("深圳市", "南山区")`；拆不开时返回 `("", text)`。

    Only used as a *retry* when the whole string fails to geocode — the normal
    path is a single `geocode("深圳南山区")`, which Amap already resolves to
    adcode 440305 at level 区县.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return "", ""

    for index, char in enumerate(cleaned):
        if char in "市州盟" and index + 1 < len(cleaned):
            head, tail = cleaned[: index + 1], cleaned[index + 1 :]
            if looks_like_district(tail):
                return head, tail
    return "", cleaned


def _text(value: Any) -> str:
    """Amap sometimes answers `[]` instead of `""` for an empty field."""
    return value if isinstance(value, str) else ""


def join_name(province: str, city: str, district: str) -> str:
    """
    `("北京市", "北京市", "朝阳区")` → `"北京市朝阳区"`（直辖市不重复）。

    The full name is deliberate: for an ambiguous bare district name it is the
    only way the reader can tell 深圳南山 from 鹤岗南山 without a follow-up
    question.
    """
    province = (province or "").strip()
    city = (city or "").strip()
    district = (district or "").strip()

    parts: list[str] = []
    if province:
        parts.append(province)
    if city and city != province:
        parts.append(city)
    if district and district not in (province, city):
        parts.append(district)
    return "".join(parts)


def _place_from_geocode(hit: dict, *, query: str, ambiguous: bool, resolved_by: str) -> dict:
    province = _text(hit.get("province"))
    city = _text(hit.get("city"))
    district = _text(hit.get("district"))
    adcode = _text(hit.get("adcode"))
    lon = hit.get("longitude")
    lat = hit.get("latitude")

    # `level` decides the announcement: only a real district keeps district
    # precision. Amap levels seen in the wild: 省 / 城市 / 区县 / 开发区 / 乡镇 / 兴趣点.
    level = "district" if district else "city"

    return {
        "query": query,
        "province": province,
        "city": city,
        "district": district,
        "adcode": adcode,
        "lon": lon,
        "lat": lat,
        "level": level,
        "displayName": join_name(province, city, district) or query,
        "ambiguous": ambiguous,
        "resolvedBy": resolved_by,
    }


def _place_from_regeo(area: dict, *, query: str) -> dict:
    province = _text(area.get("province"))
    city = _text(area.get("city"))
    district = _text(area.get("district"))
    return {
        "query": query,
        "province": province,
        "city": city,
        "district": district,
        "adcode": _text(area.get("adcode")),
        "lon": area.get("longitude"),
        "lat": area.get("latitude"),
        "level": "district" if district else "city",
        "displayName": join_name(province, city, district) or _text(area.get("formatted")) or query,
        "ambiguous": False,
        "resolvedBy": "amap-regeo",
    }


def _short_names(*values: str) -> list[str]:
    """`深圳市` → `深圳`，`广东省` → `广东`；用于判断用户是否已经给了上下文。"""
    names: list[str] = []
    for value in values:
        text = (value or "").strip()
        if len(text) >= 3 and text.endswith(("市", "省", "自治区", "特别行政区")):
            text = text.rstrip("市省")
            text = text.replace("自治区", "").replace("特别行政区", "")
        if len(text) >= 2:
            names.append(text)
    return names


def _has_context(query: str, place: dict) -> bool:
    """
    True when the user's own words already say *which* district they mean.

    「深圳南山区」「北京朝阳区」 carry the city, so a multi-hit geocode is not
    ambiguous for them. A bare 「南山区」 has no context — that is exactly the
    case that must be announced with the full province+city name.
    """
    return any(name in query for name in _short_names(place.get("city", ""), place.get("province", "")))


async def resolve_place(query: str) -> dict:
    """
    Resolve free text or a `"lon,lat"` pair into a `Place`.

    Never raises for "this name is a bit odd" — only a total geocode failure
    surfaces as `UpstreamError`, and even then callers can fall back to the raw
    string (Amap still understands a plain city name).
    """
    cleaned = (query or "").strip()
    if not cleaned:
        raise UpstreamError("MISSING_CITY", "未提供城市名")

    cached = _cache_get(cleaned)
    if cached is not None:
        return cached

    # 坐标（浏览器定位）→ 逆地理，精度直接到街道/区。
    point = parse_coords(cleaned)
    if point:
        area = await regeo(format_coords(*point))
        place = _place_from_regeo(area, query=cleaned)
        # Keep the *actual* fix, not the district centroid: the user is here now.
        place["lon"], place["lat"] = point[0], point[1]
        place["resolvedBy"] = "user-coords"
        _cache_put(cleaned, place)
        return place

    candidates = await geocode_all(cleaned)
    if not candidates:
        city_hint, district_hint = split_city_district(cleaned)
        if city_hint and district_hint and district_hint != cleaned:
            candidates = await geocode_all(district_hint, city=city_hint)
    if not candidates:
        raise UpstreamError("LOCATION_UNRESOLVED", f"无法解析地名「{cleaned}」的行政区划")

    # A bare district name legitimately has several matches (朝阳区 in 北京 and
    # 长春, 南山区 in 深圳 and 鹤岗). We take the most prominent one — Amap orders
    # by importance — but flag it so the answer prints the full province+city
    # name instead of just "南山区".
    place = _place_from_geocode(
        candidates[0],
        query=cleaned,
        ambiguous=False,
        resolved_by="amap-geocode",
    )
    place["ambiguous"] = len(candidates) > 1 and not _has_context(cleaned, place)
    _cache_put(cleaned, place)
    return place
