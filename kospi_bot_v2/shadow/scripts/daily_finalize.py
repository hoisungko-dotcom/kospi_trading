"""
kr-shadow-daily.service entry point — runs at 16:00 KST (07:00 UTC) Mon–Fri.

Reads today's validated end-of-day snapshot written by the live runner and
finalizes all shadow portfolio positions, then publishes the daily report.

Exit codes:
  0 — success, OR expected data-unavailable: snapshot absent on a weekend/holiday.
  1 — unexpected condition requiring operator attention:
        - snapshot absent on a weekday (live-runner outage or write failure)
        - snapshot is stale (session_date ≠ today on a trading day)
        - snapshot is not final (is_final=False — written before 15:30 KST)
        - programming error or corrupt state
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("shadow.daily_finalize")

_KST = timezone(timedelta(hours=9))


def _kst_today() -> date:
    """Return today's date in KST. Patched in tests to inject a specific date."""
    return datetime.now(_KST).date()


def main() -> None:
    try:
        _run()
    except _NoData as e:
        # Expected: weekend/holiday, snapshot not yet written today.
        logger.warning("No market data — expected on weekends/holidays: %s", e)
        sys.exit(0)
    except Exception as e:
        # Unexpected: outage, stale snapshot, corrupt state, programming error.
        logger.error("daily_finalize failed: %s", e, exc_info=True)
        sys.exit(1)


class _NoData(Exception):
    """Raised for known data-unavailable conditions on non-trading days (exit 0)."""


def _run() -> None:
    from kospi_bot_v2.shadow.snapshot import (
        load_and_validate_snapshot,
        NoSnapshotError,
    )
    from kospi_bot_v2.shadow.league import ShadowLeague

    base_dir = Path(os.environ.get("SHADOW_STATE_DIR", "data/shadow_league"))

    try:
        snap = load_and_validate_snapshot(base_dir)
    except NoSnapshotError as e:
        # P0-6: absent snapshot on a weekday = live-runner outage → exit 1.
        # On a weekend there is no trading session, so absence is expected → exit 0.
        today_kst = _kst_today()
        if today_kst.weekday() >= 5:  # Saturday=5, Sunday=6
            raise _NoData(e) from e
        raise RuntimeError(
            f"No EOD snapshot on weekday {today_kst} KST — "
            "live-runner outage or snapshot-write failure?"
        ) from e
    # StaleSnapshotError (session_date mismatch on trading day, or is_final=False)
    # propagates to main() → logged as error → exit 1.

    trade_date      = str(snap["trade_date"])
    regime          = str(snap["regime"])
    kospi_pct       = float(snap["kospi_pct"])
    ohlcv_by_symbol = dict(snap["ohlcv_by_symbol"])
    current_prices  = {k: float(v) for k, v in snap["prices"].items()}
    trading_day     = bool(snap.get("trading_day", True))

    league = ShadowLeague(base_dir)
    report = league.finalize_day(
        ohlcv_by_symbol=ohlcv_by_symbol,
        trade_date=trade_date,
        regime=regime,
        kospi_pct=kospi_pct,
        current_prices=current_prices,
        trading_day=trading_day,
        send_telegram=True,
    )

    logger.info("Daily finalization complete for %s (trading_day=%s)", trade_date, trading_day)
    logger.info(report)


if __name__ == "__main__":
    main()
