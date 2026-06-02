from __future__ import annotations

from kospi_bot_v2.config.settings import ShadowSettings
from kospi_bot_v2.domain.models import MarketRegime, Position


class RiskGuard:
    def __init__(self, settings: ShadowSettings):
        self.settings = settings

    def max_positions(self, regime: MarketRegime) -> int:
        if regime is MarketRegime.BULL:
            return self.settings.max_positions_bull
        if regime is MarketRegime.NEUTRAL:
            return self.settings.max_positions_neutral
        if regime is MarketRegime.WEAK:
            return self.settings.max_positions_weak
        return self.settings.max_positions_crash

    def allow_new_entry(self, regime: MarketRegime, open_positions: dict[str, Position], daily_pnl_pct: float) -> tuple[bool, str]:
        if daily_pnl_pct <= self.settings.daily_loss_limit_pct:
            return False, "daily loss limit reached"
        if len(open_positions) >= self.max_positions(regime):
            return False, "max positions for regime reached"
        return True, "allowed"
