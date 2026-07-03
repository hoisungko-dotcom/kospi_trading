from __future__ import annotations

from realtime.paper_engine import INITIAL_CASH, STATE_PATH, PaperEngine

__all__ = ["PaperEngine", "StrategyStateEngine", "INITIAL_CASH", "STATE_PATH"]


class StrategyStateEngine(PaperEngine):
    """Neutral runtime alias for the live/paper strategy state engine."""

