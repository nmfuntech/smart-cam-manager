"""Small helpers for daily time windows such as "23:00-07:00"."""

from __future__ import annotations

import re
from datetime import datetime, time

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_hhmm(value: str) -> time | None:
    match = _HHMM.match(value or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_window(value: str) -> tuple[time, time] | None:
    """Parse "HH:MM-HH:MM". Returns None for empty or malformed input."""
    if not value or "-" not in value:
        return None
    start_raw, end_raw = value.split("-", 1)
    start, end = parse_hhmm(start_raw), parse_hhmm(end_raw)
    if start is None or end is None or start == end:
        return None
    return start, end


def in_window(window: tuple[time, time] | None, now: datetime | None = None) -> bool:
    """True when ``now`` falls inside the window; windows may wrap midnight."""
    if window is None:
        return False
    current = (now or datetime.now()).time()
    start, end = window
    if start < end:
        return start <= current < end
    return current >= start or current < end
