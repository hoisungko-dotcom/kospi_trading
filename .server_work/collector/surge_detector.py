"""급등 탐지기 — 1분봉 스트림에서 급등 캔들 식별 후 직전 N캔들 반환."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_tail(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_tail(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        return self.high - self.low or 0.0001

    @property
    def pct_change(self) -> float:
        return (self.close - self.open) / self.open if self.open else 0.0


@dataclass
class SurgeEvent:
    stk_cd: str
    surge_candle: Candle
    pre_candles: list[Candle]
    surge_pct: float
    vol_ratio: float


def atr(candles: list[Candle], n: int = 5) -> float:
    if len(candles) < 2:
        return 1.0
    trs = []
    for i in range(1, min(n + 1, len(candles))):
        prev = candles[-i - 1]
        cur = candles[-i]
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs) if trs else 1.0


class SurgeDetector:
    def __init__(self, surge_pct: float = 0.03, vol_mult: float = 3.0, lookback: int = 5, vol_window: int = 20):
        self.surge_pct = surge_pct
        self.vol_mult = vol_mult
        self.lookback = lookback
        self.vol_window = vol_window

    def check(self, stk_cd: str, history: list[Candle]) -> Optional[SurgeEvent]:
        need = self.lookback + self.vol_window + 1
        if len(history) < need:
            return None

        cur = history[-1]
        prev = history[-2]
        chg = (cur.close - prev.close) / prev.close if prev.close else 0.0
        if chg < self.surge_pct:
            return None

        avg_vol = sum(c.volume for c in history[-(self.vol_window + 1):-1]) / self.vol_window
        v_ratio = cur.volume / avg_vol if avg_vol else 0.0
        if v_ratio < self.vol_mult:
            return None

        pre = history[-(self.lookback + 1):-1]
        return SurgeEvent(
            stk_cd=stk_cd,
            surge_candle=cur,
            pre_candles=pre,
            surge_pct=chg,
            vol_ratio=v_ratio,
        )

