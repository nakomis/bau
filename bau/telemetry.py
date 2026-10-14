from __future__ import annotations

import json
import re
from dataclasses import dataclass

LIMIT_TEXT = re.compile(r"hit your \w+ limit", re.IGNORECASE)


@dataclass(frozen=True)
class Window:
    utilization: float
    resets_at: int


@dataclass(frozen=True)
class RateLimit:
    status: str
    five_hour: Window | None
    seven_day: Window | None


def parse_stream_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _window(d: object) -> Window | None:
    if not isinstance(d, dict):
        return None
    try:
        return Window(float(d["utilization"]), int(d["resetsAt"]))
    except (KeyError, TypeError, ValueError):
        return None


def rate_limit_from_event(event: dict) -> RateLimit | None:
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        info = {}
    windows = info.get("unifiedWindows")
    if not isinstance(windows, dict):
        windows = {}
    return RateLimit(
        status=str(info.get("status", "unknown")),
        five_hour=_window(windows.get("five_hour")),
        seven_day=_window(windows.get("seven_day")),
    )


def result_hit_limit(result: dict) -> bool:
    return bool(result.get("is_error")) and bool(LIMIT_TEXT.search(str(result.get("result", ""))))
