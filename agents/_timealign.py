"""
Time alignment for route weather — pure functions, no I/O, no network.

Stage 3 answers a different question than stage 2:

    stage 2: "what is the weather along this route right now?"
    stage 3: "what will the weather be at each waypoint *when I get there*?"

To do that we need three things, all of which live here:

  1. **A trustworthy local clock.** China Standard Time, never UTC. Windows
     Python builds often ship without the IANA `tzdata` package, where
     `ZoneInfo("Asia/Shanghai")` raises — so we fall back to a fixed +08:00
     offset (Shanghai has had no DST since 1991, so the two are equivalent).

  2. **Arrival offsets that respect congestion.** Amap's driving response
     carries a `duration` per step, so we interpolate cumulative *time* (not
     cumulative distance) inside the span that contains each sample point.
     When step data is missing (transit/walking), we degrade to a
     distance-proportional estimate and say so in `basis`.

  3. **Nearest-hour matching.** QWeather returns hourly buckets on the hour
     (`2026-10-19T16:00+08:00`); a 15:45 arrival is answered with the 16:00
     bucket. We record the gap so the answer can be honest about it.

Everything here is deterministic and side-effect free, which is what makes the
tricky cases (cross-midnight, DST-free offset, missing step data) testable
without hitting a single upstream API.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

TZ_NAME = "Asia/Shanghai"
#: Fixed fallback offset — CST is UTC+8 all year round.
SHANGHAI_FIXED = timezone(timedelta(hours=8), TZ_NAME)

_tz_cache: timezone | Any = None


def local_tz() -> Any:
    """`Asia/Shanghai` when tzdata is present, else a fixed +08:00 offset."""
    global _tz_cache
    if _tz_cache is None:
        try:
            from zoneinfo import ZoneInfo

            _tz_cache = ZoneInfo(TZ_NAME)
        except Exception:  # missing tzdata on some Windows builds
            _tz_cache = SHANGHAI_FIXED
    return _tz_cache


def local_now() -> datetime:
    """Current time in China Standard Time, always timezone-aware."""
    return datetime.now(local_tz())


def to_local(value: datetime) -> datetime:
    """Normalise any aware datetime to the local zone (naive → assume local)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=local_tz())
    return value.astimezone(local_tz())


def parse_fx_time(value: Any) -> datetime | None:
    """
    Parse QWeather's `fxTime` (`"2026-10-19T16:00+08:00"`) into an aware dt.

    Tolerates `Z` suffixes, second precision, space separators and bare dates
    so a provider format change degrades to "no match" instead of an exception.
    """
    raw = str(value or "").strip()
    if not raw:
        return None

    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(raw[:-1] + "+00:00")

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz())
        return parsed

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=local_tz())
        except ValueError:
            continue
    return None


def format_iso(value: datetime) -> str:
    """Aware dt → `2026-10-19T15:45+08:00` (what we expose in JSON)."""
    return to_local(value).isoformat(timespec="minutes")


def format_human(value: datetime) -> str:
    """Aware dt → `2026-10-19 15:45` (what a person reads)."""
    return to_local(value).strftime("%Y-%m-%d %H:%M")


def format_clock(value: datetime) -> str:
    """Aware dt → `15:45`."""
    return to_local(value).strftime("%H:%M")


def to_qweather_hour(value: datetime) -> str:
    """
    Aware dt → `2026-10-19T15:00+08:00`.

    QWeather's v7 hourly endpoints take no time parameter (they always answer
    "from now on"), so this is for logging/debugging only — never a request arg.
    """
    local = to_local(value).replace(minute=0, second=0, microsecond=0)
    return local.isoformat(timespec="minutes")


# ========== 到达时刻 ==========
def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_spans(step_spans: Sequence[dict] | None) -> list[tuple[float, float]]:
    """Keep only spans that carry a positive distance and a usable duration."""
    spans: list[tuple[float, float]] = []
    for span in step_spans or []:
        distance = _num(span.get("distance_m"))
        duration = _num(span.get("duration_s"))
        if distance is None or duration is None:
            continue
        if distance <= 0 or duration < 0:
            continue
        spans.append((distance, duration))
    return spans


def arrival_offsets(
    *,
    total_duration_s: float | None,
    total_distance_m: float | None,
    ratios: Iterable[float],
    step_spans: Sequence[dict] | None = None,
) -> tuple[list[float], str]:
    """
    Seconds elapsed when each sample point is reached.

    Returns `(offsets_seconds, basis)` where `basis` is `"step-duration"` when
    Amap's per-step durations were usable, otherwise `"distance-proportional"`.

    Invariants guaranteed by construction (callers rely on all three):
      * `offsets[0] == 0`
      * `offsets[-1] == total_duration_s`
      * the list is monotonic non-decreasing
    """
    duration = max(_num(total_duration_s) or 0.0, 0.0)
    ratio_list = [min(max(_num(r) or 0.0, 0.0), 1.0) for r in ratios]
    if not ratio_list:
        return [], "distance-proportional"

    spans = _clean_spans(step_spans)
    if not spans or duration <= 0:
        return [duration * r for r in ratio_list], "distance-proportional"

    cumulative_distance: list[float] = []
    cumulative_time: list[float] = []
    distance_sum = 0.0
    time_sum = 0.0
    for span_distance, span_duration in spans:
        distance_sum += span_distance
        time_sum += span_duration
        cumulative_distance.append(distance_sum)
        cumulative_time.append(time_sum)

    if distance_sum <= 0 or time_sum <= 0:
        return [duration * r for r in ratio_list], "distance-proportional"

    offsets: list[float] = []
    cursor = 0
    for ratio in ratio_list:
        target = distance_sum * ratio
        while cursor < len(cumulative_distance) - 1 and cumulative_distance[cursor] < target:
            cursor += 1
        previous_distance = cumulative_distance[cursor - 1] if cursor > 0 else 0.0
        previous_time = cumulative_time[cursor - 1] if cursor > 0 else 0.0
        span_distance = cumulative_distance[cursor] - previous_distance
        span_time = cumulative_time[cursor] - previous_time
        fraction = (target - previous_distance) / span_distance if span_distance > 0 else 1.0
        fraction = min(max(fraction, 0.0), 1.0)
        offsets.append(previous_time + fraction * span_time)

    # Step durations and the path-level duration occasionally disagree (Amap
    # rounds both); rescale so the last point lands exactly on `duration`.
    if time_sum > 0 and abs(time_sum - duration) > 1.0:
        scale = duration / time_sum
        offsets = [value * scale for value in offsets]

    offsets = [min(max(value, 0.0), duration) for value in offsets]
    for index in range(1, len(offsets)):
        offsets[index] = max(offsets[index], offsets[index - 1])
    offsets[0] = 0.0
    offsets[-1] = duration
    return offsets, "step-duration"


def arrival_times(departure: datetime, offsets_s: Sequence[float]) -> list[datetime]:
    """`departure + offset` for every offset, keeping the local zone."""
    base = to_local(departure)
    return [base + timedelta(seconds=max(float(offset), 0.0)) for offset in offsets_s]


# ========== 最近整点匹配 ==========
def nearest_hour(
    entries: Sequence[dict],
    target: datetime,
    *,
    tolerance_min: int = 90,
) -> dict | None:
    """
    Pick the hourly bucket closest to `target`.

    `entries` are normalized dicts carrying a `time` field (QWeather `fxTime`).
    Ties go to the **later** bucket: arriving at 15:45, the 16:00 forecast is
    the one that describes the hour you are actually in.

    Returns `{"entry", "time", "deltaMin"}` or `None` when nothing is within
    `tolerance_min` (the signal to escalate to a wider forecast tier).
    """
    goal = to_local(target)
    best: tuple[float, float] | None = None
    best_entry: dict | None = None
    best_time: datetime | None = None

    for entry in entries or []:
        moment = parse_fx_time(entry.get("time"))
        if moment is None:
            continue
        delta_min = (moment - goal).total_seconds() / 60.0
        key = (abs(delta_min), -delta_min)
        if best is None or key < best:
            best = key
            best_entry = entry
            best_time = moment

    if best is None or best_entry is None or best_time is None:
        return None
    if best[0] > max(int(tolerance_min), 0):
        return None
    return {
        "entry": best_entry,
        "time": best_time,
        "deltaMin": round((best_time - goal).total_seconds() / 60.0, 1),
    }


def nearest_date(entries: Sequence[dict], target: datetime) -> dict | None:
    """Daily-bucket variant: match on calendar date (for `/v7/weather/7d`)."""
    goal = to_local(target).date()
    for entry in entries or []:
        raw_date = str(entry.get("date") or entry.get("time") or "")[:10]
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw_date)
        if not match:
            continue
        try:
            day = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                tzinfo=local_tz(),
            ).date()
        except ValueError:
            continue
        if day == goal:
            return {"entry": entry, "time": None, "deltaMin": None}
    return None


# ========== 预报 → 实况字段形状 ==========
def forecast_to_live(entry: dict, *, tier: str = "") -> dict:
    """
    Reshape an hourly/daily forecast entry into the `weather_live()` contract.

    Keeping the same field names is deliberate: `build_advice()` and the whole
    frontend stay untouched, and stage-2 assertions keep passing. The
    `source` field records that the values are forecast, not observation.
    """
    entry = entry or {}
    moment = parse_fx_time(entry.get("time"))

    temperature = _num(entry.get("temperature"))
    if temperature is None:
        # Daily buckets only expose a range; the midpoint is the honest single
        # number for "what will it feel like".
        low, high = _num(entry.get("tempMin")), _num(entry.get("tempMax"))
        if low is not None and high is not None:
            temperature = round((low + high) / 2.0, 1)

    humidity = _num(entry.get("humidity"))
    precip = _num(entry.get("precip"))
    pop = _num(entry.get("pop"))
    if pop is None:
        pop = _num(entry.get("popDay"))
    if precip is None:
        precip = _num(entry.get("precipDay"))

    return {
        "weather": entry.get("text") or entry.get("textDay") or "",
        "temperature": temperature,
        "humidity": humidity,
        "winddirection": entry.get("windDir") or entry.get("windDirDay") or "",
        "windpower": entry.get("windScale") or entry.get("windScaleDay") or "",
        # Amap's `_report_hour()` expects `"YYYY-MM-DD HH:MM:SS"`; give it that
        # shape so advice keeps using the right hour of day.
        "reporttime": moment.strftime("%Y-%m-%d %H:%M:%S") if moment else "",
        "precip": precip,
        "pop": pop,
        "source": f"qweather-{tier}" if tier else "qweather",
        "forecast": True,
    }
