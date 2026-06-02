from __future__ import annotations

import os

from kospi_bot_v2.config.settings import ShadowSettings
from kospi_bot_v2.domain.models import MarketRegime, Signal


class PositionSizer:
    def __init__(self, settings: ShadowSettings):
        self.settings = settings

    def quantity(self, cash: float, equity: float, signal: Signal, regime: MarketRegime) -> int:
        if signal.price <= 0 or cash <= 0:
            return 0

        pct = self.settings.base_position_pct
        if regime is MarketRegime.CRASH:
            pct *= 0.35

        multiplier = float(signal.metadata.get("position_multiplier", 1.0) or 1.0)
        pct = min(self.settings.max_position_pct, pct) * multiplier
        budget = equity * pct
        min_budget = cash * float(os.getenv("V2_MIN_POSITION_PCT", "0.18") or 0.18)
        max_budget = cash * float(os.getenv("V2_MAX_SINGLE_POSITION_PCT", "0.30") or 0.30)
        if signal.price <= max_budget:
            budget = max(budget, min_budget, signal.price)
        budget = min(cash, max_budget, budget)
        return max(0, int(budget // signal.price))
