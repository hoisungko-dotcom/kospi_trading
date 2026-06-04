"""
Strategy definitions for the KR Shadow Strategy League.

Rules (immutable after deployment):
- Never modify an existing (strategy_id, version) definition.
- To change behavior, add a new version: B-v2, C-v2, etc.
- strategy_id "E" must NEVER generate a trade.
- A-v1 must EXACTLY match the current deployed live bot (true control).

Current deployed live bot settings (from config/settings.py):
  stop_loss_pct       = -0.020
  take_profit_pct     =  0.050
  trailing_start_pct  =  0.025
  trailing_gap_pct    =  0.010
  time_stop (shadow)  =  1 day  (live bot uses 45 min; shadow uses EOD)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ExitParams:
    stop_pct: float
    take_pct: float
    trail_start_pct: float
    trail_gap_pct: float
    max_hold_days: int


class BaseShadowStrategy(ABC):
    strategy_id: str
    version: str
    description: str

    @abstractmethod
    def should_enter(self, row: pd.Series, regime: str) -> bool:
        """Return True if this strategy wants to enter on this raw market frame row."""

    @abstractmethod
    def exit_params(self) -> ExitParams:
        """Fixed exit parameters for this strategy version."""

    def _f(self, row: pd.Series, key: str, default: float = 0.0) -> float:
        v = row.get(key, default)
        return float(v) if pd.notna(v) else default

    def score(self, row: pd.Series) -> float:
        """Candidate priority score (higher = preferred when daily entry limit applies)."""
        close     = self._f(row, "close", 1)
        sma20     = self._f(row, "sma20", close)
        sma60     = self._f(row, "sma60", close)
        high20    = self._f(row, "high20", close)
        avg_vol20 = self._f(row, "avg_volume20", 1)
        volume    = self._f(row, "volume", 0)
        rsi       = self._f(row, "rsi14", 50)
        return5   = self._f(row, "return5", 0)
        sma5      = self._f(row, "sma5", close)
        atr_pct   = self._f(row, "atr_pct", 0.02)
        vol_ratio = volume / max(avg_vol20, 1)

        trend = (10 if close > sma20 else 0) + (7 if sma20 >= sma60 else 0)
        vol_sc = min(20, max(0, (vol_ratio - 0.8) * 25))
        mom = (12 if 48 <= rsi <= 68 else 7 if 42 <= rsi <= 76 else 0)
        stab = max(0, 10 - atr_pct * 100)
        breakout_sc = max(0, 1 - abs(close / max(high20, 1) - 1)) * 18
        pullback_sc = (15 if close > sma20 and close <= sma5 * 1.03 and return5 < -0.01 else 0)
        return trend + vol_sc + mom + stab + breakout_sc + pullback_sc


# ─────────────────────────────────────────────────────────────────────────────
# A-v1 — Near-control: APPROXIMATION of current deployed live bot behavior
#         Entry: signal_engine._strategy_for() logic, DEFENSE_LONG disabled
#         Exit:  deployed defaults (-2% stop / +5% take / +2.5% trail / 1% gap)
#         Hold:  max 1 trading day EOD (live bot uses 45-min intraday time stop)
#         NOTE:  Not an exact replica — the EOD time stop produces different outcomes
#                than the live 45-min stop, especially on intraday reversals.
# ─────────────────────────────────────────────────────────────────────────────

class StrategyA_v1(BaseShadowStrategy):
    strategy_id = "A"
    version     = "v1"
    # P1-1: A-v1 is an approximation of the live bot, not an exact replica.
    # The live bot uses a 45-minute intraday time stop; shadow uses EOD (1 trading day).
    # This makes A-v1 a near-control, not a true control. Interpret comparisons accordingly.
    description = "실전봇 근사 기준점 (DEFENSE_LONG 제외, 시간손절 EOD로 근사)"

    def should_enter(self, row: pd.Series, regime: str) -> bool:
        close     = self._f(row, "close", 0)
        sma20     = self._f(row, "sma20", close)
        sma60     = self._f(row, "sma60", close)
        high20    = self._f(row, "high20", close)
        volume    = self._f(row, "volume", 0)
        avg_vol20 = self._f(row, "avg_volume20", max(volume, 1))
        rsi       = self._f(row, "rsi14", 50)
        return5   = self._f(row, "return5", 0)
        return20  = self._f(row, "return20", 0)
        cbd       = int(self._f(row, "consecutive_buy_days", 0))
        vol_ratio = volume / max(avg_vol20, 1)

        if regime == "CRASH":
            # Strong BREAKOUT only in CRASH (DEFENSE_LONG disabled)
            return (high20 > 0 and close >= high20 * 0.99
                    and avg_vol20 > 0 and volume >= avg_vol20 * 2.0
                    and rsi <= 72 and return20 <= 0.40)

        vol_surge = avg_vol20 > 0 and volume >= avg_vol20 * 1.5
        if high20 > 0 and close >= high20 * 0.995 and vol_surge:
            return True                                          # BREAKOUT
        if cbd >= 3:
            return True                                          # MOMENTUM
        if sma20 > 0 and sma60 > 0 and close > sma20 > sma60 and return5 < -0.01:
            return True                                          # PULLBACK strict
        if (close > sma20 and sma20 >= sma60 * 0.98
                and -0.06 <= return5 <= 0.08 and -0.05 <= return20 <= 0.30
                and 42 <= rsi <= 70 and vol_ratio >= 0.75):
            return True                                          # PULLBACK relaxed
        return False

    def exit_params(self) -> ExitParams:
        # Exact deployed defaults from config/settings.py
        return ExitParams(stop_pct=-0.020, take_pct=0.050,
                          trail_start_pct=0.025, trail_gap_pct=0.010,
                          max_hold_days=1)


# ─────────────────────────────────────────────────────────────────────────────
# A-v2 — Exit experiment: wider stop/take (-4%/+8%, trail+5%)
#         Same entry logic as A-v1, different exit hypothesis.
# ─────────────────────────────────────────────────────────────────────────────

class StrategyA_v2(BaseShadowStrategy):
    strategy_id = "A2"
    version     = "v2"
    description = "A 진입, 넓은 청산 실험 (-4%/+8%)"

    _entry = StrategyA_v1()

    def should_enter(self, row: pd.Series, regime: str) -> bool:
        return self._entry.should_enter(row, regime)

    def exit_params(self) -> ExitParams:
        return ExitParams(stop_pct=-0.040, take_pct=0.080,
                          trail_start_pct=0.050, trail_gap_pct=0.025,
                          max_hold_days=5)

    def score(self, row: pd.Series) -> float:
        return self._entry.score(row)


# ─────────────────────────────────────────────────────────────────────────────
# B-v1 — BULL-only Trend Pullback (strict)
# ─────────────────────────────────────────────────────────────────────────────

class StrategyB_v1(BaseShadowStrategy):
    strategy_id = "B"
    version     = "v1"
    description = "상승추세 눌림목 (BULL 전용)"

    def should_enter(self, row: pd.Series, regime: str) -> bool:
        if regime != "BULL":
            return False
        close     = self._f(row, "close", 0)
        sma20     = self._f(row, "sma20", close)
        sma60     = self._f(row, "sma60", close)
        high20    = self._f(row, "high20", close)
        sma20_sl  = self._f(row, "sma20_slope", 0)
        sma60_sl  = self._f(row, "sma60_slope", 0)
        return5   = self._f(row, "return5", 0)
        return20  = self._f(row, "return20", 0)
        rsi       = self._f(row, "rsi14", 50)
        vol_ratio = self._f(row, "volume_ratio", 0)

        return (
            sma20 > 0 and sma60 > 0
            and close > sma20 > sma60
            and sma20_sl > 0 and sma60_sl > 0
            and return20 >= 0.02
            and -0.07 <= return5 <= -0.005
            and close > sma60 * 1.01
            and 38 <= rsi <= 65
            and vol_ratio >= 0.60
            and not (high20 > 0 and close >= high20 * 0.99)  # not a breakout
        )

    def exit_params(self) -> ExitParams:
        return ExitParams(stop_pct=-0.050, take_pct=0.100,
                          trail_start_pct=0.060, trail_gap_pct=0.030,
                          max_hold_days=7)


# ─────────────────────────────────────────────────────────────────────────────
# C-v1 — Breakout ALL regimes (regime recorded per trade for analysis)
# ─────────────────────────────────────────────────────────────────────────────

class StrategyC_v1(BaseShadowStrategy):
    strategy_id = "C"
    version     = "v1"
    description = "거래량 돌파 — 전 레짐 (레짐별 분석)"

    def should_enter(self, row: pd.Series, _regime: str) -> bool:
        close    = self._f(row, "close", 0)
        high20   = self._f(row, "high20", close)
        vol_r    = self._f(row, "volume_ratio", 0)
        rsi      = self._f(row, "rsi14", 50)
        return20 = self._f(row, "return20", 0)

        return (
            high20 > 0
            and close >= high20 * 0.995
            and vol_r >= 1.5
            and rsi <= 72
            and return20 <= 0.40
        )

    def exit_params(self) -> ExitParams:
        return ExitParams(stop_pct=-0.050, take_pct=0.150,
                          trail_start_pct=0.080, trail_gap_pct=0.040,
                          max_hold_days=7)

    def score(self, row: pd.Series) -> float:
        vol_r = self._f(row, "volume_ratio", 1.0)
        rs20  = self._f(row, "rs20", 0.0)
        return vol_r * (1 + max(rs20, 0))


# ─────────────────────────────────────────────────────────────────────────────
# C-v2 — Exceptional Breakout outside BULL (stricter filters)
# ─────────────────────────────────────────────────────────────────────────────

class StrategyC_v2(BaseShadowStrategy):
    strategy_id = "C2"
    version     = "v2"
    description = "초강력 돌파 (비BULL 전용, 엄격)"

    def should_enter(self, row: pd.Series, regime: str) -> bool:
        if regime == "BULL":
            return False                             # B-v1 covers BULL pullback
        close    = self._f(row, "close", 0)
        high20   = self._f(row, "high20", close)
        vol_r    = self._f(row, "volume_ratio", 0)
        rsi      = self._f(row, "rsi14", 50)
        return20 = self._f(row, "return20", 0)
        rs20     = self._f(row, "rs20", -999)       # relative strength vs KOSPI

        return (
            high20 > 0
            and close >= high20 * 0.998              # tighter: must be at/above high20
            and vol_r >= 2.5                         # stronger: 2.5× avg volume
            and rsi <= 68
            and return20 <= 0.30
            and rs20 >= 0.03                         # must outperform KOSPI by 3%+ in 20d
        )

    def exit_params(self) -> ExitParams:
        return ExitParams(stop_pct=-0.060, take_pct=0.180,
                          trail_start_pct=0.090, trail_gap_pct=0.045,
                          max_hold_days=7)

    def score(self, row: pd.Series) -> float:
        vol_r = self._f(row, "volume_ratio", 1.0)
        rs20  = self._f(row, "rs20", 0.0)
        return vol_r * (1 + max(rs20, 0)) * 1.2     # slight priority over C-v1


# ─────────────────────────────────────────────────────────────────────────────
# D-v1 — B-v1 + valid SMA224 gate
#         IMPORTANT: requires 260+ rows of history. Skips gracefully if unavailable.
#         P1-5: D-v1 is INACTIVE until the live data pipeline provides valid SMA224
#         values computed from at least 260 rows of daily history. Until then, every
#         row returns sma224=NaN/None/0 and D-v1 will generate zero entries.
# ─────────────────────────────────────────────────────────────────────────────

class StrategyD_v1(BaseShadowStrategy):
    strategy_id = "D"
    version     = "v1"
    description = "SMA224 게이트 눌림목 (장기 추세 필터) — SMA224 이력 확보 전 비활성"

    _b = StrategyB_v1()

    def should_enter(self, row: pd.Series, regime: str) -> bool:
        if not self._b.should_enter(row, regime):
            return False
        close  = self._f(row, "close", 0)
        sma224 = row.get("sma224", None)

        # Guard: NaN or missing SMA224 means lookback is insufficient → skip
        if sma224 is None or pd.isna(sma224) or float(sma224) <= 0:
            return False                             # logged externally in league.evaluate()

        return close > float(sma224)

    def exit_params(self) -> ExitParams:
        return self._b.exit_params()

    def score(self, row: pd.Series) -> float:
        return self._b.score(row)


# ─────────────────────────────────────────────────────────────────────────────
# E-v1 — Cash benchmark: NEVER trades
# ─────────────────────────────────────────────────────────────────────────────

class StrategyE_v1(BaseShadowStrategy):
    strategy_id = "E"
    version     = "v1"
    description = "현금 보유 기준점 (무거래)"

    def should_enter(self, _row: pd.Series, _regime: str) -> bool:
        return False

    def exit_params(self) -> ExitParams:
        return ExitParams(0, 0, 0, 0, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Registry — do NOT remove entries; add new versions only
# ─────────────────────────────────────────────────────────────────────────────

ALL_STRATEGIES: list[BaseShadowStrategy] = [
    StrategyA_v1(),
    StrategyA_v2(),
    StrategyB_v1(),
    StrategyC_v1(),
    StrategyC_v2(),
    StrategyD_v1(),
    StrategyE_v1(),
]

STRATEGY_BY_ID: dict[str, BaseShadowStrategy] = {
    s.strategy_id: s for s in ALL_STRATEGIES
}

# Frozen expected registry — if this fails at import time, a strategy was changed
_EXPECTED = {
    "A":  "v1",
    "A2": "v2",
    "B":  "v1",
    "C":  "v1",
    "C2": "v2",
    "D":  "v1",
    "E":  "v1",
}
assert {s.strategy_id: s.version for s in ALL_STRATEGIES} == _EXPECTED, (
    "Strategy registry changed. Add a new version instead of modifying an existing one."
)
