from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from kospi_bot_v2.config.settings import ShadowSettings


def now_in_active_timezone(settings: ShadowSettings) -> datetime:
    return datetime.now(ZoneInfo(settings.active_timezone))


def hhmm(dt: datetime) -> int:
    return dt.hour * 100 + dt.minute


def is_active_time(settings: ShadowSettings, now: datetime | None = None) -> bool:
    current = now or now_in_active_timezone(settings)
    if current.weekday() >= 5:
        return False
    current_hhmm = hhmm(current)
    return settings.active_start_hhmm <= current_hhmm < settings.active_end_hhmm
