from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass
class Box:
    high: float
    low: float
    formed_at: str
    length: int


class BoxLadderExit:
    """Track nested/upstairs boxes and exit when the active structure breaks."""

    def __init__(
        self,
        entry_price: float,
        entry_box_high: float,
        entry_box_low: float,
        timeframe: str = "1min",
    ) -> None:
        self.entry_price = float(entry_price)
        self.timeframe = timeframe
        self.max_hold_bars = int(os.getenv("BOX_LADDER_MAX_HOLD_BARS", "120"))
        self.abs_stoploss_pct = float(os.getenv("BOX_LADDER_ABS_STOPLOSS_PCT", "3.0")) / 100.0
        self.new_box_min_length = int(os.getenv("BOX_LADDER_NEW_BOX_MIN_LENGTH", "5"))
        self.new_box_max_height = float(os.getenv("BOX_LADDER_NEW_BOX_MAX_HEIGHT", "5.0"))
        self.max_stack = int(os.getenv("BOX_LADDER_MAX_STACK", "5"))
        self.hold_bars = 0
        self._box_stack = [
            Box(
                high=float(entry_box_high),
                low=float(entry_box_low),
                formed_at="entry",
                length=self.new_box_min_length,
            )
        ]
        self._active_box_index = 0

    def update(self, candles: list[Any]) -> tuple[bool, str]:
        if not candles:
            return False, "hold"

        self.hold_bars += 1
        current_close = float(candles[-1].close)

        if current_close <= self.entry_price * (1.0 - self.abs_stoploss_pct):
            return True, "absolute_stoploss"

        if self.hold_bars >= self.max_hold_bars:
            return True, "max_hold"

        new_box = self._detect_new_box(candles)
        if new_box is not None:
            self._box_stack.append(new_box)
            if len(self._box_stack) > self.max_stack:
                self._box_stack = self._box_stack[-self.max_stack:]
            self._active_box_index = len(self._box_stack) - 1
            return False, "new_box_formed"

        for idx in range(len(self._box_stack) - 1, -1, -1):
            box = self._box_stack[idx]
            if current_close >= box.low:
                self._active_box_index = idx
                return False, "hold"

        return True, "box_breakdown"

    def _detect_new_box(self, candles: list[Any]) -> Box | None:
        if len(candles) < self.new_box_min_length + 1:
            return None

        active_high = self._box_stack[self._active_box_index].high
        current_ts = getattr(candles[-1], "ts", f"idx-{len(candles) - 1}")
        for existing in self._box_stack:
            if existing.formed_at == current_ts:
                return None

        max_length = min(len(candles) - 1, 20)
        for length in range(max_length, self.new_box_min_length - 1, -1):
            window = candles[-(length + 1):-1]
            box_high = max(float(c.high) for c in window)
            box_low = min(float(c.low) for c in window)
            if box_low <= 0:
                continue
            if box_low <= active_high:
                continue
            height_pct = (box_high - box_low) / box_low * 100.0
            if height_pct < 0.6 or height_pct > self.new_box_max_height:
                continue
            return Box(high=box_high, low=box_low, formed_at=current_ts, length=length)
        return None

    @property
    def active_box_low(self) -> float:
        return self._box_stack[self._active_box_index].low

    @property
    def box_stack(self) -> list[dict[str, float | str | int]]:
        return [
            {
                "high": box.high,
                "low": box.low,
                "formed_at": box.formed_at,
                "length": box.length,
            }
            for box in self._box_stack
        ]
