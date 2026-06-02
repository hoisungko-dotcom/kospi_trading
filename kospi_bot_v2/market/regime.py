from __future__ import annotations

from kospi_bot_v2.domain.models import MarketRegime, MarketSnapshot


class RegimeClassifier:
    """Classify broad market condition before individual stock selection."""

    def classify(self, snapshot: MarketSnapshot) -> MarketRegime:
        weak_points = 0
        if snapshot.kospi_change_pct <= -0.7:
            weak_points += 1
        if snapshot.kosdaq_change_pct <= -0.9:
            weak_points += 1
        if snapshot.advance_ratio < 0.35:
            weak_points += 1
        if snapshot.volatility_pct > 2.2:
            weak_points += 1
        if not snapshot.kospi_above_sma20:
            weak_points += 1
        if not snapshot.kosdaq_above_sma20:
            weak_points += 1

        if weak_points >= 5:
            return MarketRegime.CRASH
        if weak_points >= 3:
            return MarketRegime.WEAK

        strong_points = 0
        if snapshot.kospi_change_pct > 0.3:
            strong_points += 1
        if snapshot.kosdaq_change_pct > 0.4:
            strong_points += 1
        if snapshot.advance_ratio > 0.55:
            strong_points += 1
        if snapshot.kospi_above_sma20:
            strong_points += 1
        if snapshot.kosdaq_above_sma20:
            strong_points += 1

        if strong_points >= 4:
            return MarketRegime.BULL
        return MarketRegime.NEUTRAL
