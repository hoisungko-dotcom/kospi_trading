from __future__ import annotations

import pandas as pd

from kospi_bot_v2.config.settings import ShadowSettings
from kospi_bot_v2.domain.models import MarketRegime, Signal, SignalAction, StrategyType
from kospi_bot_v2.strategy.gates import CandidateGates


_GRADE_MULTIPLIER = {
    "A": 1.0,
    "B": 0.5,
    "C": 0.3,
}


class SignalEngine:
    """Market-regime aware stock and inverse ETF signal engine."""

    def __init__(self, settings: ShadowSettings):
        self.settings = settings
        self.gates = CandidateGates()

    def generate(self, frame: pd.DataFrame, regime: MarketRegime) -> list[Signal]:
        latest = frame.sort_values("timestamp").groupby("symbol").tail(1).copy()
        if "close_prev" not in latest:
            latest["close_prev"] = frame.sort_values("timestamp").groupby("symbol")["close"].shift()

        signals: list[Signal] = []
        for _, row in latest.iterrows():
            reject = self.gates.reject_reason(row, regime)
            if reject:
                continue

            strategy = self._strategy_for(row, regime)
            if strategy is None:
                continue
            score, reason = self._score(row, regime, strategy)
            grade = self._grade(score)
            if grade is None:
                continue
            if strategy is StrategyType.PULLBACK and grade == "C" and not self._allow_c_pullback(row, score, regime):
                continue
            if score >= self._threshold(regime, strategy):
                position_multiplier = self._position_multiplier(strategy, grade)
                signals.append(
                    Signal(
                        action=SignalAction.BUY,
                        symbol=str(row["symbol"]),
                        name=str(row["name"]),
                        strategy=strategy,
                        score=round(score, 2),
                        reason=reason,
                        price=float(row["close"]),
                        metadata={
                            "grade": grade,
                            "position_multiplier": position_multiplier,
                            "rsi14": float(row.get("rsi14", 50)),
                            "return5": float(row.get("return5", 0) or 0),
                            "return20": float(row.get("return20", 0) or 0),
                            "consecutive_buy_days": int(row.get("consecutive_buy_days", 0) or 0),
                            "atr_pct": float(row.get("atr_pct", 0.02) or 0.02),
                            "regime": regime.value,
                        },
                    )
                )

        return sorted(signals, key=lambda item: item.score, reverse=True)

    def diagnose(self, frame: pd.DataFrame, regime: MarketRegime) -> list[dict[str, object]]:
        latest = frame.sort_values("timestamp").groupby("symbol").tail(1).copy()
        rows: list[dict[str, object]] = []
        for _, row in latest.iterrows():
            symbol = str(row["symbol"])
            if symbol.startswith(("KSPI", "KDQ")):
                continue
            reject = self.gates.reject_reason(row, regime)
            strategy = None if reject else self._strategy_for(row, regime)
            score = None
            grade = None
            reason = ""
            status = "reject"
            if not reject and strategy is None:
                status = "no_strategy"
            elif strategy is not None:
                score, reason = self._score(row, regime, strategy)
                grade = self._grade(score)
                status = "signal" if grade and score >= self._threshold(regime, strategy) else "below_threshold"
            rows.append(
                {
                    "symbol": symbol,
                    "name": str(row["name"]),
                    "status": status,
                    "reject": reject or "",
                    "strategy": strategy.value if strategy else "",
                    "score": round(score, 2) if score is not None else None,
                    "grade": grade or "",
                    "return5": float(row.get("return5", 0) or 0),
                    "return20": float(row.get("return20", 0) or 0),
                    "rsi14": float(row.get("rsi14", 50) or 50),
                    "volume_ratio": float(row["volume"]) / max(float(row.get("avg_volume20", row["volume"]) or row["volume"]), 1),
                    "atr_pct": float(row.get("atr_pct", 0.02) or 0.02),
                    "reason": reason,
                }
            )
        return sorted(
            rows,
            key=lambda item: (
                item["status"] != "signal",
                -(item["score"] or 0),
                item["symbol"],
            ),
        )

    def _threshold(self, regime: MarketRegime, strategy: StrategyType | None = None) -> float:
        if regime is MarketRegime.CRASH:
            return 70
        if regime is MarketRegime.WEAK and strategy in {StrategyType.PULLBACK, StrategyType.DEFENSE_LONG}:
            return 70
        if regime is MarketRegime.BULL:
            return self.settings.min_score_bull
        if regime is MarketRegime.NEUTRAL:
            return self.settings.min_score_neutral
        return self.settings.min_score_weak

    def _allow_c_pullback(self, row: pd.Series, score: float, regime: MarketRegime) -> bool:
        if regime not in {MarketRegime.WEAK, MarketRegime.CRASH}:
            return False
        return5 = float(row.get("return5", 0) or 0)
        return20 = float(row.get("return20", 0) or 0)
        rsi = float(row.get("rsi14", 50) or 50)
        atr_pct = float(row.get("atr_pct", 0.02) or 0.02)
        return (
            score >= 72
            and -0.02 <= return5 <= 0.06
            and -0.05 <= return20 <= 0.25
            and 45 <= rsi <= 62
            and atr_pct <= 0.12
        )

    def _strategy_for(self, row: pd.Series, regime: MarketRegime) -> StrategyType | None:
        close = float(row["close"])
        sma5 = float(row.get("sma5", close) or close)
        sma20 = float(row.get("sma20", close) or close)
        sma60 = float(row.get("sma60", close) or close)
        high20 = float(row.get("high20", close) or close)
        volume = float(row.get("volume", 0) or 0)
        avg_volume20 = float(row.get("avg_volume20", volume) or volume)
        rsi = float(row.get("rsi14", 50) or 50)
        return5 = float(row.get("return5", 0) or 0)
        return20 = float(row.get("return20", 0) or 0)
        atr_pct = float(row.get("atr_pct", 0.02) or 0.02)
        consecutive_buy_days = int(row.get("consecutive_buy_days", 0) or 0)

        volume_ratio = volume / max(avg_volume20, 1)
        if regime in {MarketRegime.WEAK, MarketRegime.CRASH}:
            if close > sma20 and close >= sma5 * 0.995 and sma20 >= sma60 * 0.98 and -0.08 <= return5 <= 0.06 and -0.05 <= return20 <= 0.35 and 42 <= rsi <= 64 and atr_pct <= 0.08 and volume_ratio >= 0.65:
                return StrategyType.DEFENSE_LONG
            if regime is MarketRegime.CRASH:
                if close > sma20 and close >= sma5 * 0.995 and volume_ratio >= 0.75 and 45 <= rsi <= 68 and -0.03 <= return5 <= 0.08 and -0.05 <= return20 <= 0.40 and atr_pct <= 0.12:
                    return StrategyType.DEFENSE_LONG
                volume_surge_crash = avg_volume20 > 0 and volume >= avg_volume20 * 2.0
                if high20 > 0 and close >= high20 * 0.99 and volume_surge_crash and rsi <= 72 and return20 <= 0.40:
                    return StrategyType.BREAKOUT
                return None

        volume_surge = avg_volume20 > 0 and volume >= avg_volume20 * 1.5
        if high20 > 0 and close >= high20 * 0.995 and volume_surge:
            return StrategyType.BREAKOUT
        if consecutive_buy_days >= 3:
            return StrategyType.MOMENTUM
        if sma20 > 0 and sma60 > 0 and close > sma20 > sma60 and return5 < -0.01:
            return StrategyType.PULLBACK
        if close > sma20 and sma20 >= sma60 * 0.98 and -0.06 <= return5 <= 0.08 and -0.05 <= return20 <= 0.30 and 42 <= rsi <= 70 and volume_ratio >= 0.75:
            return StrategyType.PULLBACK
        return None

    def _score(self, row: pd.Series, regime: MarketRegime, strategy: StrategyType) -> tuple[float, str]:
        close = float(row["close"])
        sma5 = float(row.get("sma5", close) or close)
        sma20 = float(row.get("sma20", close) or close)
        sma60 = float(row.get("sma60", close) or close)
        high20 = float(row.get("high20", close) or close)
        avg_volume20 = float(row.get("avg_volume20", row["volume"]) or row["volume"])
        rsi = float(row.get("rsi14", 50) or 50)
        macd = float(row.get("macd", 0) or 0)
        macd_signal = float(row.get("macd_signal", 0) or 0)
        return5 = float(row.get("return5", 0) or 0)
        return20 = float(row.get("return20", 0) or 0)
        atr_pct = float(row.get("atr_pct", 0.02) or 0.02)
        consecutive_buy_days = int(row.get("consecutive_buy_days", 0) or 0)

        trend = 0
        if close > sma20:
            trend += 10
        if sma5 > sma20:
            trend += 8
        if sma20 >= sma60:
            trend += 7

        volume = min(20, max(0, (float(row["volume"]) / max(avg_volume20, 1) - 0.8) * 25))
        momentum = 0
        if 48 <= rsi <= 68:
            momentum += 12
        elif 42 <= rsi <= 76:
            momentum += 7
        if macd > macd_signal:
            momentum += 8

        breakout = max(0, 1 - abs(close / max(high20, 1) - 1)) * 18
        if avg_volume20 > 0 and float(row["volume"]) >= avg_volume20 * 1.5:
            breakout += 7
        pullback = 15 if close > sma20 and close <= sma5 * 1.03 and return5 < -0.01 else 0
        if close > sma20 and close <= sma5 * 1.04 and -0.06 <= return5 <= 0.08 and 42 <= rsi <= 70:
            pullback += 12
        momentum_follow = min(18, consecutive_buy_days * 4)
        if 0 <= return20 <= 0.18:
            momentum_follow += 5
        stability = max(0, 10 - atr_pct * 100)

        score = trend + volume + momentum + stability
        if strategy is StrategyType.BREAKOUT:
            score += breakout
        elif strategy is StrategyType.MOMENTUM:
            score += momentum_follow
        elif strategy is StrategyType.PULLBACK:
            score += pullback
        elif strategy is StrategyType.DEFENSE_LONG:
            score += 18 + stability
            if -0.04 <= return5 <= 0.04:
                score += 8
            if 42 <= rsi <= 62:
                score += 6
        parts = [
            f"strategy={strategy.value}",
            f"grade={self._grade(score) or 'D'}",
            f"trend={trend}",
            f"volume={volume:.1f}",
            f"momentum={momentum}",
            f"stability={stability:.1f}",
            f"return5={return5:.1%}",
            f"return20={return20:.1%}",
        ]
        return score, ", ".join(parts)

    def _grade(self, score: float) -> str | None:
        if score >= 85:
            return "A"
        if score >= 75:
            return "B"
        if score >= 65:
            return "C"
        return None

    def _position_multiplier(self, strategy: StrategyType, grade: str) -> float:
        multiplier = _GRADE_MULTIPLIER[grade]
        if strategy in {StrategyType.BREAKOUT, StrategyType.MOMENTUM} and grade == "B":
            return 0.5
        return multiplier
