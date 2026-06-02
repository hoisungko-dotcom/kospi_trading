from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MarketRegime(str, Enum):
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    WEAK = "WEAK"
    CRASH = "CRASH"


class StrategyType(str, Enum):
    BREAKOUT = "BREAKOUT"
    MOMENTUM = "MOMENTUM"
    PULLBACK = "PULLBACK"
    DEFENSE_LONG = "DEFENSE_LONG"
    INVERSE_ETF = "INVERSE_ETF"


class SignalAction(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Candle:
    symbol: str
    name: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp: datetime
    kospi_change_pct: float
    kosdaq_change_pct: float
    advance_ratio: float
    volatility_pct: float
    kospi_above_sma20: bool
    kosdaq_above_sma20: bool


@dataclass(frozen=True)
class Signal:
    action: SignalAction
    symbol: str
    name: str
    strategy: StrategyType
    score: float
    reason: str
    price: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    name: str
    strategy: StrategyType
    entry_time: datetime
    entry_price: float
    quantity: int
    peak_price: float
    stop_price: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def pnl_pct(self, price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (price / self.entry_price) - 1.0


@dataclass(frozen=True)
class Trade:
    timestamp: datetime
    symbol: str
    name: str
    action: SignalAction
    strategy: StrategyType
    price: float
    quantity: int
    pnl_pct: float | None
    reason: str
