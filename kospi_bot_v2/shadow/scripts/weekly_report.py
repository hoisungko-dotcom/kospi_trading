"""
kr-shadow-weekly.service entry point — runs at 16:30 KST (07:30 UTC) every Friday.

Reads the most recent EOD snapshot for current prices, then generates and
publishes the weekly strategy league ranking report.

Exit codes:
  0 — success, OR expected no-data condition (snapshot not yet available).
  1 — unexpected programming or configuration error.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("shadow.weekly_report")


def main() -> None:
    try:
        _run()
    except _NoData as e:
        logger.warning("No market data available for weekly report: %s", e)
        sys.exit(0)
    except Exception as e:
        logger.error("weekly_report failed unexpectedly: %s", e, exc_info=True)
        sys.exit(1)


class _NoData(Exception):
    """Raised for known data-unavailable conditions (exit 0)."""


def _run() -> None:
    from kospi_bot_v2.shadow.snapshot import load_snapshot, NoSnapshotError
    from kospi_bot_v2.shadow.league import ShadowLeague

    base_dir = Path(os.environ.get("SHADOW_STATE_DIR", "data/shadow_league"))

    try:
        snap = load_snapshot(base_dir)
    except NoSnapshotError as e:
        raise _NoData(e) from e

    current_prices = {k: float(v) for k, v in snap["prices"].items()}

    league = ShadowLeague(base_dir)

    today = date.today()
    iso = today.isocalendar()
    week_label = f"{iso[0]}년 {iso[1]}주차"

    monday = today - timedelta(days=today.weekday())
    period_str = f"{monday.isoformat()} ~ {today.isoformat()}"

    trading_days_elapsed = _count_trading_days(league)
    next_eval = (today + timedelta(days=7)).isoformat()

    report = league.weekly_report(
        week_label=week_label,
        period_str=period_str,
        trading_days_elapsed=trading_days_elapsed,
        next_eval_date=next_eval,
        current_prices=current_prices,
        send_telegram=True,
    )

    logger.info("Weekly report complete for %s", week_label)
    logger.info(report)


def _count_trading_days(league) -> int:
    """Count unique trading days from NAV history across all portfolios."""
    all_dates: set[str] = set()
    for p in league.portfolios:
        for snap in p.nav_history:
            all_dates.add(snap["date"])
    return len(all_dates)


if __name__ == "__main__":
    main()
