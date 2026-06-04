"""
Shadow league integration shim for the live monitoring loop.

Call site in live_runner.py:
    from kospi_bot_v2.shadow.runner_integration import shadow_evaluate

Rules:
- Never raises: all exceptions are caught and logged.
- Never blocks the live monitoring loop.
- Never imports live_broker in this module; the live runner owns that.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

_league = None  # lazy singleton — initialized on first call


def _get_league():
    global _league
    if _league is None:
        from kospi_bot_v2.shadow.league import ShadowLeague
        base_dir = Path(os.getenv("SHADOW_STATE_DIR", "data/shadow_league"))
        _league = ShadowLeague(base_dir)
        logger.info("Shadow league initialized at %s", base_dir)
    return _league


def shadow_evaluate(
    frame: "pd.DataFrame",
    regime_str: str,
    prices: dict[str, float],
    timestamp: datetime,
    trade_date: str | None = None,
) -> None:
    """Evaluate shadow entries and exits. Safe to call every intraday loop.

    Failures are caught and logged — they must not block or crash the live loop.
    The caller (live_runner) passes the same frame and prices it already computed
    for its own evaluation.
    """
    try:
        _get_league().evaluate(frame, regime_str, prices, timestamp, trade_date)
    except Exception as e:
        logger.error("shadow_evaluate failed (live loop unaffected): %s", e, exc_info=True)


def shadow_evaluate_exits(
    prices: dict[str, float],
    timestamp: datetime,
    trade_date: str | None = None,
) -> None:
    """Check shadow exits against current prices. Safe to call every loop."""
    try:
        _get_league().evaluate_exits(prices, timestamp, trade_date)
    except Exception as e:
        logger.error("shadow_evaluate_exits failed (live loop unaffected): %s", e, exc_info=True)
