"""Compatibility wrapper for the canonical runtime market-hours path."""

from runtime.market_hours import hhmm, is_active_time, now_in_active_timezone

__all__ = ["hhmm", "is_active_time", "now_in_active_timezone"]
