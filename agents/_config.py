"""
Configuration loader — private module (starts with `_`), not mapped as a route.

Single source of truth for every environment variable this Agent reads.

Why a dedicated module:
  * `agents/chat/index.py` calls `load_dotenv()` at line ~30, but it imports
    `.._tools` BEFORE that line runs. Any `os.getenv()` executed at import time
    of `_tools` would therefore see an empty environment. Loading dotenv here,
    at the very top of the import chain, removes that ordering hazard.
  * Validation / defaults / type coercion live in one place, so a missing key
    produces one clear diagnostic instead of scattered `None` errors.

Naming rule: the accessor names are NOT aliases for the env var names — the
env var names themselves are the contract (EdgeOne Makers injects `.env` line
by line as real environment variables), and they are used verbatim here.

Security: no function in this module ever returns a credential to a caller
that could echo it into a tool result, an SSE frame, or a log line. Use
`mask()` if a key must appear in a diagnostic string.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Idempotent; does not override variables already present in the real
# environment (override=False is the default), which is what we want:
# platform-injected values win over a stale local .env.
load_dotenv()

# ========== Defaults ==========
DEFAULT_CITY_FALLBACK = "北京"
DEFAULT_HOURLY_PROVIDER = "qweather"
DEFAULT_ROUTE_SAMPLE_POINTS = 4
ROUTE_SAMPLE_POINTS_MIN = 2
ROUTE_SAMPLE_POINTS_MAX = 10
DEFAULT_FORECAST_TOLERANCE_MIN = 90
FORECAST_TOLERANCE_MIN = 30
FORECAST_TOLERANCE_MAX = 180

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}
_FALSY = {"0", "false", "no", "off", "n", "f"}


def _raw(name: str) -> str:
    """Read a variable, returning a stripped string ('' when unset)."""
    return (os.getenv(name) or "").strip()


# ========== 高德 (Amap) ==========
def amap_weather_key() -> str:
    """Key used for the weather endpoints. Falls back to the generic key."""
    return _raw("AMAP_WEATHER_API_KEY") or _raw("AMAP_API_KEY")


def amap_route_key() -> str:
    """Key used for routing/geocoding. Falls back to the weather key."""
    return _raw("AMAP_ROUTE_API_KEY") or amap_weather_key()


def amap_key() -> str:
    """Generic Amap key (used by diagnostics only)."""
    return _raw("AMAP_API_KEY") or amap_weather_key()


# ========== 默认城市 ==========
def default_city() -> str:
    """City used when the user says "here" / "my location" without naming one."""
    return _raw("DEFAULT_CITY") or DEFAULT_CITY_FALLBACK


# ========== 逐小时预报 (QWeather) ==========
def hourly_provider() -> str:
    """Which backend serves the hourly forecast. Only 'qweather' is wired up."""
    return (_raw("HOURLY_FORECAST_PROVIDER") or DEFAULT_HOURLY_PROVIDER).lower()


def qweather_key() -> str:
    return _raw("QWEATHER_API_KEY")


def qweather_host() -> str:
    """
    Bare host, e.g. `nd5u9yjya2.re.qweatherapi.com`.

    `.env` stores it WITHOUT a scheme, so callers must go through
    `qweather_base_url()` rather than pasting the host into a URL directly.
    """
    host = _raw("QWEATHER_API_HOST")
    if not host:
        return ""
    # Be forgiving if someone pastes a full URL into the variable.
    for prefix in ("https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    return host.strip("/")


def qweather_base_url() -> str:
    """`https://<host>` — empty string when the host is not configured."""
    host = qweather_host()
    return f"https://{host}" if host else ""


# ========== 沿途天气 (阶段二) ==========
def route_weather_enabled() -> bool:
    value = _raw("ROUTE_WEATHER_ENABLED").lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    # Unset or unparseable → feature off (fail closed).
    return False


def route_sample_points() -> int:
    """
    Number of waypoints to sample along a route.

    Injected as a *string* by the platform, so coerce and clamp here rather
    than trusting upstream arithmetic.
    """
    value = _raw("ROUTE_WEATHER_SAMPLE_POINTS")
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ROUTE_SAMPLE_POINTS
    return max(ROUTE_SAMPLE_POINTS_MIN, min(ROUTE_SAMPLE_POINTS_MAX, n))


# ========== 沿途天气时间对齐 (阶段三) ==========
def route_forecast_enabled() -> bool:
    """
    Kill switch for stage 3's time alignment.

    Unset → **on** (it is the current default behaviour). Setting it to any
    falsy value restores stage 2 exactly — each waypoint reports live
    conditions — which is the fastest safe rollback if QWeather misbehaves.
    """
    value = _raw("ROUTE_FORECAST_ENABLED").lower()
    if value in _FALSY:
        return False
    return True


def route_forecast_tolerance_min() -> int:
    """
    How far an hourly bucket may sit from the estimated arrival before we
    escalate to a wider forecast tier. Clamped to 30–180 minutes.
    """
    value = _raw("ROUTE_FORECAST_TOLERANCE_MIN")
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_FORECAST_TOLERANCE_MIN
    return max(FORECAST_TOLERANCE_MIN, min(FORECAST_TOLERANCE_MAX, minutes))


# ========== 区县级实况 (阶段五) ==========
def district_weather_enabled() -> bool:
    """
    Whether `get_weather` may use district-level precision (QWeather `now`).

    Unset → **on**. Turning it off restores the stage-4 behaviour exactly
    (Amap only, city-level when the district lookup fails), which is the safe
    rollback if the QWeather quota runs low.
    """
    value = _raw("DISTRICT_WEATHER_ENABLED").lower()
    if value in _FALSY:
        return False
    return True


# ========== Diagnostics ==========
def mask(secret: str | None) -> str:
    """
    Render a credential safe for logs / tool output: length + last 2 chars only.

    Never returns enough material to reconstruct the key. Empty/missing values
    are reported as `<unset>`.
    """
    if not secret:
        return "<unset>"
    if len(secret) <= 4:
        return f"<set,{len(secret)} chars>"
    return f"<set,{len(secret)} chars,...{secret[-2:]}>"


def config_status() -> dict:
    """
    Non-sensitive snapshot of the weather-related configuration.

    Used by diagnostics. Every value here is either a literal, a boolean, an
    int, or the output of `mask()` — safe to print.
    """
    return {
        "default_city": default_city(),
        "amap_key": mask(amap_key()),
        "amap_weather_key": mask(amap_weather_key()),
        "amap_route_key": mask(amap_route_key()),
        "hourly_provider": hourly_provider(),
        "qweather_key": mask(qweather_key()),
        "qweather_host": qweather_host() or "<unset>",
        "route_weather_enabled": route_weather_enabled(),
        "route_sample_points": route_sample_points(),
        "route_forecast_enabled": route_forecast_enabled(),
        "route_forecast_tolerance_min": route_forecast_tolerance_min(),
        "district_weather_enabled": district_weather_enabled(),
    }


def missing_keys() -> list[str]:
    """Return the names of variables required for the stage-1 features."""
    missing: list[str] = []
    if not amap_weather_key():
        missing.append("AMAP_WEATHER_API_KEY")
    if hourly_provider() == "qweather":
        if not qweather_key():
            missing.append("QWEATHER_API_KEY")
        if not qweather_host():
            missing.append("QWEATHER_API_HOST")
    return missing
