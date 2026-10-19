"""
Agent Tools — private module (starts with _), not mapped as a route.

Five tools are exposed to the model:

  ┌────────────────────────┬──────────────────────────────────────────────┐
  │ get_weather            │ 高德 /v3/weather/weatherInfo (extensions=base)│
  │ get_hourly_forecast    │ 和风 /v7/weather/24h 逐小时                   │
  │ get_travel_advice      │ 实况 + 逐小时 → 确定性规则引擎（含穿衣建议）    │
  │ translate_text         │ 模板自带（保留）                              │
  │ text_statistics        │ 模板自带（保留）                              │
  └────────────────────────┴──────────────────────────────────────────────┘

`get_clothing_advice` was folded into `get_travel_advice` — clothing advice is
one field of the advice bundle, not a separate capability, so the model no
longer needs a two-hop call (weather → clothing) to answer "穿什么".

Hard contract for all three weather tools:
  * Always return a JSON **string**.
  * Never raise. A provider failure becomes `{"ok": false, "error": {...}}`
    so the model can explain the problem instead of the run crashing.
  * Never echo a credential. Upstream messages are sanitized before being
    placed in a tool result.

The `city` argument is optional on every weather tool: when the user says
"我这里 / 当前位置 / 附近" without naming a place, the model omits it and we
fall back to `DEFAULT_CITY` from `.env`, flagging the substitution in the
result so the answer can be transparent about it.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from agents import function_tool

from ._advice import build_advice
from ._amap import UpstreamError, weather_live
from ._config import default_city, route_sample_points, route_weather_enabled
from ._qweather import hourly_forecast

_MAX_HOURS = 24
_MIN_HOURS = 1


# ========== Shared helpers ==========
def _resolve_city(city: str | None) -> tuple[str, bool]:
    """
    Return `(city_to_query, assumed_default)`.

    An empty/blank argument means "here" — the caller (model) had no city to
    offer, so we substitute the configured default and tell the truth about it.
    """
    cleaned = (city or "").strip()
    if cleaned:
        return cleaned, False
    return default_city(), True


def _sanitize(message: str) -> str:
    """
    Defensive scrub before a message lands in a tool result.

    Upstream error text should never contain a key, but a provider echoing a
    malformed request URL is not impossible — so strip anything shaped like a
    credential parameter or a bare 32/64-char hex token.
    """
    scrubbed = re.sub(r"(?i)(key|token|secret|api_?key)\s*[=:]\s*[^\s&,\"']+", r"\1=***", str(message))
    scrubbed = re.sub(r"\b[0-9a-fA-F]{32,}\b", "***", scrubbed)
    return scrubbed


def _ok(tool: str, **fields: Any) -> str:
    return json.dumps({"ok": True, "tool": tool, **fields}, ensure_ascii=False)


def _fail(tool: str, code: str, message: str) -> str:
    return json.dumps(
        {"ok": False, "tool": tool, "error": {"code": code, "message": _sanitize(message)}},
        ensure_ascii=False,
    )


def _wrap_upstream(tool: str, exc: UpstreamError) -> str:
    return _fail(tool, exc.code, exc.message)


# ========== Tool: Get Weather (实况) ==========
@function_tool
async def get_weather(
    city: Annotated[
        str,
        "城市名，如「北京」「上海」。若无法确定用户所在城市（如用户只说「我这」「当前位置」），请传空字符串 \"\"。",
    ] = "",
) -> str:
    """查询指定城市的实时天气（温度、天气现象、风向风力、湿度、发布时间）。"""
    tool = "get_weather"
    try:
        query_city, assumed = _resolve_city(city)
        live = await weather_live(query_city)
        return _ok(
            tool,
            query={
                "city": query_city,
                "resolvedCity": live.get("city", ""),
                "assumedDefaultCity": assumed,
            },
            current=live,
            source="amap",
            note="数据来自高德地图实时天气接口 /v3/weather/weatherInfo",
        )
    except UpstreamError as exc:
        return _wrap_upstream(tool, exc)
    except Exception as exc:  # noqa: BLE001 — a tool must never crash the run
        return _fail(tool, "UNEXPECTED", f"{type(exc).__name__}: {exc}")


# ========== Tool: Get Hourly Forecast (逐小时) ==========
@function_tool
async def get_hourly_forecast(
    city: Annotated[
        str,
        "城市名，如「北京」。若无法确定用户所在城市，请传空字符串 \"\"。",
    ] = "",
    hours: Annotated[int, "需要的小时数，1–24，默认 12。"] = 12,
) -> str:
    """查询指定城市未来若干小时的逐小时预报（温度、天气现象、降水概率、降水量的逐时变化）。"""
    tool = "get_hourly_forecast"
    try:
        query_city, assumed = _resolve_city(city)
        try:
            hours_int = int(hours)
        except (TypeError, ValueError):
            hours_int = 12
        hours_int = max(_MIN_HOURS, min(_MAX_HOURS, hours_int))

        result = await hourly_forecast(query_city, hours_int)
        return _ok(
            tool,
            query={
                "city": query_city,
                "assumedDefaultCity": assumed,
                "hours": hours_int,
            },
            available=bool(result.get("available")),
            reason=result.get("reason", ""),
            provider=result.get("provider", "qweather"),
            location=result.get("location", ""),
            resolvedBy=result.get("resolvedBy", ""),
            updateTime=result.get("updateTime", ""),
            hourly=result.get("hourly", []),
            source="qweather",
            note="数据来自和风天气逐小时接口 /v7/weather/24h" if result.get("available")
            else "逐小时数据不可用，请在回答中说明并只使用实况信息。",
        )
    except UpstreamError as exc:
        return _wrap_upstream(tool, exc)
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, "UNEXPECTED", f"{type(exc).__name__}: {exc}")


# ========== Tool: Get Travel Advice (出行建议，含穿衣) ==========
@function_tool
async def get_travel_advice(
    city: Annotated[
        str,
        "城市名，如「北京」。若无法确定用户所在城市，请传空字符串 \"\"。",
    ] = "",
) -> str:
    """
    基于实时天气与逐小时预报，给出确定性的出行建议：穿衣、是否带伞、防晒、洗车、户外运动、风力与能见度提醒。
    这是「穿什么」「要不要带伞」这类问题的首选工具。
    """
    tool = "get_travel_advice"
    try:
        query_city, assumed = _resolve_city(city)

        # 实况是必需的；逐小时是增强项，失败不影响建议主体。
        live = await weather_live(query_city)

        hourly_result = await hourly_forecast(query_city, 12)
        hourly_list = hourly_result.get("hourly") if hourly_result.get("available") else None

        advice = build_advice(live, hourly_list)

        return _ok(
            tool,
            query={
                "city": query_city,
                "resolvedCity": live.get("city", ""),
                "assumedDefaultCity": assumed,
            },
            current=live,
            hourly={
                "available": bool(hourly_result.get("available")),
                "reason": hourly_result.get("reason", ""),
                "provider": hourly_result.get("provider", "qweather"),
                "updateTime": hourly_result.get("updateTime", ""),
                "hours": hourly_list or [],
            },
            advice=advice,
            routeWeather={
                "enabled": route_weather_enabled(),
                "samplePoints": route_sample_points(),
                "stage": "阶段二功能，本阶段仅返回配置状态，不做沿途采样",
            },
            source="rule-engine",
            note=(
                "advice 字段由确定性规则引擎产出，结论与数值不可改写；"
                "请只对 advice.summary 做语言润色，不要新增或修改任何建议结论。"
            ),
        )
    except UpstreamError as exc:
        return _wrap_upstream(tool, exc)
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, "UNEXPECTED", f"{type(exc).__name__}: {exc}")


# ========== Tool: Translate Text (模板自带，保留) ==========
@function_tool
def translate_text(
    text: Annotated[str, "The text to translate"],
    target_language: Annotated[str, "Target language code, e.g. en, ja, fr, ko, de"],
) -> str:
    """Translate text to the specified language."""
    # TODO: Replace with real translation API (e.g. DeepL, Google Translate)
    language_names = {
        "en": "English",
        "ja": "日本語",
        "fr": "Français",
        "ko": "한국어",
        "de": "Deutsch",
        "es": "Español",
        "ru": "Русский",
    }
    lang_name = language_names.get(target_language, target_language)
    return json.dumps(
        {"ok": True, "tool": "translate_text", "targetLanguage": target_language,
         "translation": f"[Mock translation to {lang_name}]: {text}",
         "note": "模板自带的 mock 实现，尚未接入真实翻译服务。"},
        ensure_ascii=False,
    )


# ========== Tool: Text Statistics (模板自带，保留) ==========
@function_tool
def text_statistics(text: Annotated[str, "The text to analyze"]) -> str:
    """Analyze text and return statistics like character count and word count."""
    char_count = len(text)
    word_count = len(text.split())
    line_count = len(text.split("\n"))
    return json.dumps(
        {"ok": True, "tool": "text_statistics",
         "charCount": char_count, "wordCount": word_count, "lineCount": line_count},
        ensure_ascii=False,
    )
