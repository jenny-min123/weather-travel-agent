"""
Travel-advice rule engine — private module, not a route.

Deliberately deterministic: every conclusion below is produced by an explicit
threshold, never by a language model. The LLM's only job downstream is to
*polish the wording* of the `summary` field. This keeps advice reproducible
(the same weather always yields the same recommendation), auditable, and
independent of model mood.

Consumed by `_tools.get_travel_advice`, which combines:
    _amap.weather_live()   → current conditions
    _qweather.hourly_forecast() → next N hours (optional, may be unavailable)
and then calls `build_advice()`.

Pure functions, no I/O — safe to unit test.
"""

from __future__ import annotations

import re
from typing import Any

# ========== Vocabulary ==========
_RAIN_MARKERS = ("雨",)  # 小雨/中雨/大雨/暴雨/阵雨/雷阵雨/雨夹雪 all contain 雨
_SNOW_MARKERS = ("雪", "冰粒", "霰")
_STORM_MARKERS = ("雷", "冰雹")
_LOW_VISIBILITY_MARKERS = ("雾", "霾", "沙", "尘", "浮尘", "扬沙")
_SUNNY_MARKERS = ("晴",)
_HAZE_MARKERS = ("霾", "雾", "沙", "尘")


def _text_of(current: dict) -> str:
    """Weather description for the current observation."""
    return str(current.get("weather") or "")


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


def _is_raining(text: str) -> bool:
    return _contains_any(text, _RAIN_MARKERS)


def _is_snowing(text: str) -> bool:
    return _contains_any(text, _SNOW_MARKERS)


def _max_wind_scale(windpower: str) -> int | None:
    """
    Parse Amap's `windpower` into an integer Beaufort scale.

    Observed formats: `"≤3"`, `"3"`, `"3-4"`, `"1-3"`, `"4"`, `"5-6"`.
    We take the upper bound, because advice should be driven by the worst
    case the user may encounter.
    """
    if not windpower:
        return None
    digits = re.findall(r"\d+", str(windpower))
    if not digits:
        return None
    try:
        return max(int(d) for d in digits)
    except ValueError:
        return None


def _hour_label(fx_time: str) -> str:
    """`"2026-10-16T18:00+08:00"` → `"18:00"`."""
    if not fx_time:
        return ""
    if "T" in fx_time:
        time_part = fx_time.split("T", 1)[1]
    else:
        time_part = fx_time
    match = re.match(r"(\d{2}:\d{2})", time_part)
    return match.group(1) if match else time_part[:5]


def _report_hour(reporttime: str) -> int | None:
    """Extract the hour (0-23) from Amap's `"2026-10-16 17:03:54"`."""
    if not reporttime:
        return None
    match = re.search(r"\b(\d{2}):\d{2}:\d{2}\b", reporttime)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


# ========== 穿衣建议 ==========
# (下限, 等级, 建议, 要点)
_CLOTHING_BANDS: list[tuple[float, str, str, list[str]]] = [
    (-100.0, "极寒", "羽绒服、厚毛衣、加绒裤，戴帽子手套围巾，尽量减少户外停留。",
     ["羽绒服", "厚毛衣", "加绒裤", "帽子", "手套", "围巾"]),
    (-10.0, "严寒", "厚羽绒服或棉服 + 保暖内层，注意头部与手部保暖。",
     ["羽绒服/棉服", "保暖内衣", "围巾", "手套"]),
    (0.0, "寒冷", "厚外套或薄羽绒，里面加一件毛衣，早晚温差注意。",
     ["厚外套/薄羽绒", "毛衣", "长裤"]),
    (8.0, "偏冷", "外套加针织衫，怕冷的话可以带条围巾。",
     ["外套", "针织衫", "长裤"]),
    (15.0, "微凉", "薄外套或卫衣即可，内搭长袖更灵活。",
     ["薄外套/卫衣", "长袖"]),
    (20.0, "舒适", "长袖衬衫或薄卫衣正合适，早晚可备一件薄外套。",
     ["长袖衬衫/薄卫衣", "可选薄外套"]),
    (26.0, "偏热", "短袖 + 轻薄下装，注意补水。",
     ["短袖", "薄长裤/短裤"]),
    (30.0, "炎热", "透气短袖短裤，尽量避开正午户外活动，多补水。",
     ["透气球衣", "短裤", "遮阳帽"]),
    (35.0, "酷热", "轻薄透气的浅色衣物 + 遮阳帽墨镜，避免 11:00–16:00 户外停留，随身补水。",
     ["浅色透气衣", "遮阳帽", "墨镜", "水杯"]),
]


def clothing_advice(temperature: float | None) -> dict:
    """Map a temperature onto a clothing recommendation."""
    if temperature is None:
        return {
            "level": "未知",
            "items": [],
            "text": "未能取到实时气温，建议出行前再确认一次天气。",
        }
    band = _CLOTHING_BANDS[0]
    for candidate in _CLOTHING_BANDS:
        if temperature >= candidate[0]:
            band = candidate
    _, level, text, items = band
    return {"level": level, "items": items, "text": text}


# ========== 逐小时降水窗口 ==========
def _analyze_hourly(hourly: list[dict] | None) -> dict:
    """
    Find the first wet window in the hourly series.

    A hour counts as wet when the provider says precipitation is expected
    (`precip > 0`) or the probability of precipitation reaches 50%.

    The two triggers are kept apart on purpose: "有雨" (the provider names a
    rain/snow phenomenon) and "降水概率高" (nothing but a high POP number) are
    different claims, and the advice text must not conflate them.
    """
    if not hourly:
        return {"available": False, "rainWindow": None, "wetHours": 0,
                "tempRange": None, "rainHint": "", "summary": "逐小时预报不可用。"}

    wet_labels: list[str] = []
    precip_texts: list[str] = []
    temps: list[float] = []
    wet_by_text = False
    wet_by_value = False
    max_precip = 0.0
    max_pop = 0.0

    for item in hourly:
        label = _hour_label(str(item.get("time", "")))
        text = str(item.get("text", "") or "")
        precip = item.get("precip")
        pop = item.get("pop")

        try:
            precip_val = float(precip) if precip is not None else 0.0
        except (TypeError, ValueError):
            precip_val = 0.0
        try:
            pop_val = float(pop) if pop is not None else 0.0
        except (TypeError, ValueError):
            pop_val = 0.0

        text_wet = _is_raining(text) or _is_snowing(text)
        value_wet = precip_val > 0 or pop_val >= 50

        if text_wet:
            wet_by_text = True
            if text and text not in precip_texts:
                precip_texts.append(text)
        if value_wet:
            wet_by_value = True

        if text_wet or value_wet:
            if label:
                wet_labels.append(label)
            max_precip = max(max_precip, precip_val)
            max_pop = max(max_pop, pop_val)

        temp = item.get("temperature")
        if isinstance(temp, (int, float)):
            temps.append(float(temp))

    rain_window = None
    if wet_labels:
        rain_window = wet_labels[0] if len(wet_labels) == 1 else f"{wet_labels[0]}–{wet_labels[-1]}"

    # Wording must match the evidence we actually have.
    if rain_window and wet_by_text:
        kinds = "、".join(precip_texts[:3]) or "降水"
        rain_hint = f"{rain_window} 有{kinds}"
    elif rain_window and max_precip > 0:
        rain_hint = f"{rain_window} 有降水（峰值约 {max_precip:.1f} mm/h）"
    elif rain_window:
        rain_hint = f"{rain_window} 降水概率升至 {max_pop:.0f}%"
    else:
        rain_hint = ""

    temp_range = None
    if temps:
        temp_range = {"min": min(temps), "max": max(temps)}

    summary = f"未来 {len(hourly)} 小时内，{rain_hint}。" if rain_hint else f"未来 {len(hourly)} 小时内无明显降水。"
    if temp_range:
        summary += f"气温 {temp_range['min']:.0f}–{temp_range['max']:.0f}°C。"

    return {
        "available": True,
        "rainWindow": rain_window,
        "wetHours": len(wet_labels),
        "tempRange": temp_range,
        "rainHint": rain_hint,
        "summary": summary,
    }


# ========== 主入口 ==========
def build_advice(current: dict, hourly: list[dict] | None = None) -> dict:
    """
    Produce the full deterministic advice bundle.

    `current` is the normalized dict from `_amap.weather_live`; `hourly` is the
    `hourly` list from `_qweather.hourly_forecast` (may be None).
    """
    weather_text = _text_of(current)
    temperature = current.get("temperature")
    humidity = current.get("humidity")
    windpower = str(current.get("windpower", "") or "")
    wind_scale = _max_wind_scale(windpower)
    report_hour = _report_hour(str(current.get("reporttime", "") or ""))

    raining_now = _is_raining(weather_text)
    snowing_now = _is_snowing(weather_text)
    storm_now = _contains_any(weather_text, _STORM_MARKERS)
    low_vis_now = _contains_any(weather_text, _LOW_VISIBILITY_MARKERS)

    hourly_info = _analyze_hourly(hourly)
    wet_ahead = bool(hourly_info.get("rainWindow"))

    # ---- 穿衣 ----
    clothing = clothing_advice(temperature if isinstance(temperature, (int, float)) else None)

    # ---- 带伞 ----
    umbrella = {"needed": False, "level": "不需要", "reason": ""}
    if raining_now:
        umbrella = {
            "needed": True,
            "level": "必备",
            "reason": f"当前天气为「{weather_text}」，出门请带伞。",
        }
    elif snowing_now:
        umbrella = {
            "needed": False,
            "level": "不建议用伞",
            "reason": f"当前天气为「{weather_text}」，建议穿防滑防水鞋、戴帽子，雨伞不实用。",
        }
    elif storm_now:
        umbrella = {"needed": True, "level": "必备", "reason": "有雷暴天气，带伞并注意避雷。"}
    elif wet_ahead:
        umbrella = {
            "needed": True,
            "level": "建议携带",
            "reason": f"{hourly_info['rainHint']}，折叠伞备一把更稳妥。",
        }
    else:
        umbrella = {"needed": False, "level": "不需要", "reason": "当前与近期均无降水迹象。"}

    # ---- 防晒 ----
    sunscreen = {"needed": False, "reason": "紫外线风险低。"}
    if _contains_any(weather_text, _SUNNY_MARKERS) and isinstance(temperature, (int, float)):
        daytime = report_hour is None or 8 <= report_hour < 17
        if daytime and temperature >= 18:
            sunscreen = {
                "needed": True,
                "reason": f"晴天下气温 {temperature:.0f}°C，建议涂防晒并戴帽子墨镜。",
            }
        elif temperature >= 28:
            sunscreen = {
                "needed": True,
                "reason": "气温偏高且天空状况好，即便非正午也建议做好防晒。",
            }

    # ---- 洗车 ----
    car_wash = {"advised": True, "reason": "未来无明显降水，可安排洗车。"}
    if raining_now or snowing_now or wet_ahead:
        cause = "当前有降水" if (raining_now or snowing_now) else hourly_info["rainHint"]
        car_wash = {"advised": False, "reason": f"{cause}，洗车后容易被淋脏。"}
    elif wind_scale is not None and wind_scale >= 5:
        car_wash = {"advised": False, "reason": f"风力达 {wind_scale} 级，扬尘较明显，洗车效果不佳。"}

    # ---- 户外运动 ----
    sport = {"advised": True, "reason": "天气条件适合户外运动。"}
    if raining_now or snowing_now or storm_now:
        sport = {"advised": False, "reason": f"当前为「{weather_text}」，建议改为室内运动。"}
    elif low_vis_now:
        sport = {"advised": False, "reason": f"当前为「{weather_text}」，能见度／空气质量不佳，不建议户外剧烈运动。"}
    elif isinstance(temperature, (int, float)) and temperature > 32:
        sport = {"advised": False, "reason": f"气温 {temperature:.0f}°C 偏高，户外运动有中暑风险。"}
    elif isinstance(temperature, (int, float)) and temperature < 5:
        sport = {"advised": False, "reason": f"气温仅 {temperature:.0f}°C，户外运动注意保暖或改为室内。"}
    elif wind_scale is not None and wind_scale >= 6:
        sport = {"advised": False, "reason": f"风力达 {wind_scale} 级，户外运动体验和安全都受影响。"}

    # ---- 能见度 / 空气 ----
    visibility = {"alert": False, "reason": ""}
    if _contains_any(weather_text, _HAZE_MARKERS):
        visibility = {
            "alert": True,
            "reason": f"当前为「{weather_text}」，能见度或空气质量欠佳，驾车请减速并开雾灯，敏感人群减少外出。",
        }

    # ---- 风力 ----
    # Amap returns directions like "东北", "南", and occasionally "静风"/"无风向" —
    # the latter already end in 风, so the suffix must not be appended blindly.
    raw_dir = str(current.get("winddirection", "") or "").strip()
    if not raw_dir:
        dir_part = ""
    elif raw_dir.endswith("风"):
        dir_part = raw_dir + " "
    else:
        dir_part = f"{raw_dir}风 "

    lead = f"{dir_part}{windpower} 级" if dir_part else f"风力 {windpower} 级"
    wind = {"scale": wind_scale, "raw": windpower, "direction": raw_dir, "text": ""}
    if wind_scale is None:
        wind["text"] = f"风向 {raw_dir or '未知'}，但未取到风力等级。"
    elif wind_scale <= 3:
        wind["text"] = f"{lead}，体感温和。"
    elif wind_scale <= 5:
        wind["text"] = f"{lead}，体感有风，注意护好帽子。"
    elif wind_scale <= 7:
        wind["text"] = f"{lead}，风力较大，高空作业与骑行需谨慎。"
    else:
        wind["text"] = f"{lead}，大风天气，尽量避免外出。"

    # ---- 湿度 ----
    humidity_note = ""
    if isinstance(humidity, (int, float)):
        if humidity <= 30:
            humidity_note = f"相对湿度仅 {humidity:.0f}%，空气干燥，注意补水和保湿。"
        elif humidity >= 85:
            humidity_note = f"相对湿度 {humidity:.0f}%，较为潮湿，注意通风与衣物晾晒。"
        else:
            humidity_note = f"相对湿度 {humidity:.0f}%，体感较为舒适。"

    # ---- 汇总 ----
    parts: list[str] = []
    if temperature is not None:
        parts.append(
            f"{current.get('city') or '当前城市'}实况 {weather_text or '未知'}，气温 {temperature:.0f}°C"
            + (f"，相对湿度 {humidity:.0f}%" if isinstance(humidity, (int, float)) else "")
            + "。"
        )
    else:
        parts.append(f"{current.get('city') or '当前城市'}实况 {weather_text or '未知'}。")
    parts.append(f"穿衣：{clothing['text']}")
    parts.append(f"雨具：{umbrella['level']}——{umbrella['reason']}")
    if sunscreen["needed"]:
        parts.append(f"防晒：{sunscreen['reason']}")
    if wind["text"]:
        parts.append(f"风力：{wind['text']}")
    if humidity_note:
        parts.append(humidity_note)
    parts.append(f"户外运动：{'适宜' if sport['advised'] else '不适宜'}——{sport['reason']}")
    parts.append(f"洗车：{'适宜' if car_wash['advised'] else '不适宜'}——{car_wash['reason']}")
    if visibility["alert"]:
        parts.append(f"提醒：{visibility['reason']}")
    if hourly_info["available"]:
        parts.append(hourly_info["summary"])

    return {
        "city": current.get("city") or "",
        "basedOn": {
            "weather": weather_text,
            "temperature": temperature,
            "humidity": humidity,
            "winddirection": current.get("winddirection", ""),
            "windpower": windpower,
            "reporttime": current.get("reporttime", ""),
        },
        "clothing": clothing,
        "umbrella": umbrella,
        "sunscreen": sunscreen,
        "carWash": car_wash,
        "outdoorSport": sport,
        "visibility": visibility,
        "wind": wind,
        "hourly": hourly_info,
        "summary": " ".join(parts),
        "generatedBy": "rule-engine",  # 确定性产出，未经模型改写
    }
