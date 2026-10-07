"""Figure out which digest slot (9 AM / 2 PM / 8 PM local) a run belongs to.

GitHub cron runs in UTC and doesn't know about daylight saving, so the workflow
fires at both possible UTC hours for each slot. This decides whether the current
run is a real slot and returns a key used to avoid sending the same slot twice.
"""
from __future__ import annotations

from datetime import datetime

TOLERANCE_MIN = 110  # GitHub can start scheduled runs late; accept up to ~2h after the slot


def current_slot(now_local: datetime, hours: list[int], tolerance_min: int = TOLERANCE_MIN) -> str | None:
    minutes = now_local.hour * 60 + now_local.minute
    for h in sorted(hours):
        if 0 <= minutes - h * 60 <= tolerance_min:
            return f"{now_local:%Y-%m-%d}@{h:02d}"
    return None


def slot_label(slot_key: str) -> str:
    hour = int(slot_key.split("@")[1])
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12} {suffix}"
