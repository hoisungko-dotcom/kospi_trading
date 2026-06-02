from __future__ import annotations

import pandas as pd

from kospi_bot_v2.domain.models import MarketRegime


class CandidateGates:
    """Hard filters before scoring.

    The gates are deliberately explainable. Every rejected row carries a reason
    that can be included in shadow reports later.
    """

    blocked_symbols = {"252670", "251340", "114800", "253160"}

    def reject_reason(self, row: pd.Series, regime: MarketRegime) -> str | None:
        symbol = str(row["symbol"])
        if symbol.startswith(("KSPI", "KDQ")):
            return "index row"
        if symbol in self.blocked_symbols:
            return "derivative ETF removed"
        min_volume_ratio = 0.45 if regime in {MarketRegime.WEAK, MarketRegime.CRASH} else 0.55
        if row.get("avg_volume20", 0) and row["volume"] < row["avg_volume20"] * min_volume_ratio:
            return "volume too weak"
        if row.get("return5", 0) > 0.25:
            return "five-day move overheated"
        close = float(row.get("close", 0) or 0)
        sma5 = float(row.get("sma5", close) or close)
        return5 = float(row.get("return5", 0) or 0)
        return20 = float(row.get("return20", 0) or 0)
        if return20 > 0.60:
            return "twenty-day move overheated"
        if return20 > 0.45 and return5 <= 0.08:
            return "late-stage trend rollover"
        if return20 > 0.25 and close < sma5 * 0.985:
            return "post-run pullback"
        max_atr = 0.14 if regime in {MarketRegime.WEAK, MarketRegime.CRASH} else 0.10
        if row.get("atr_pct", 0.02) > max_atr:
            return "volatility too high"
        if row.get("rsi14", 50) >= 82:
            return "rsi overheated"
        gap_pct = (row["open"] / row.get("close_prev", row["open"]) - 1.0) if row.get("close_prev") else 0.0
        if gap_pct > 0.10:
            return "opening gap too high"
        return None
