"""
End-of-day market snapshot I/O for the shadow league finalization.

The live runner calls save_snapshot() after each run_once(), passing:
  - session_date: the market data's date in KST (used to detect holidays)
  - is_final: True only after 15:30 KST (post-close cutoff)

The daily_finalize script calls load_and_validate_snapshot() which rejects
stale or incomplete snapshots before processing EOD exits or NAV records.

Holiday policy (P0-4):
  A KRX holiday produces a snapshot with trading_day=False and
  session_date = prior trading session. load_and_validate_snapshot()
  accepts this: session_date ≠ trade_date is only an error when trading_day=True.
  is_final=True is always required (both trading days and holidays).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
# Post-close cutoff: snapshots generated before this time are not considered final.
_FINAL_CUTOFF_HOUR_KST = 15
_FINAL_CUTOFF_MINUTE_KST = 30


class NoSnapshotError(Exception):
    """No snapshot file exists for the requested date.

    On a weekday this may mean live-runner outage (operator action required).
    On a weekend the daily timer should not fire at all.
    """


class StaleSnapshotError(Exception):
    """Snapshot exists but is rejected by freshness/finality validation.

    This is an unexpected condition (programming error or scheduling problem)
    that requires operator attention — callers should exit nonzero.
    """


def _kst_today_iso() -> str:
    """Return today's date in KST as an ISO string. Always use this instead of
    date.today() so that AWS UTC hosts use the correct Korean market date."""
    return datetime.now(KST).date().isoformat()


def snapshot_path(base_dir: Path, trade_date: str) -> Path:
    return base_dir / f"eod_snapshot_{trade_date}.json"


def save_snapshot(
    base_dir: Path,
    trade_date: str,
    regime: str,
    kospi_pct: float,
    ohlcv_by_symbol: dict,
    prices: dict[str, float],
    trading_day: bool = True,
    session_date: str | None = None,
    is_final: bool | None = None,
) -> Path:
    """Persist an end-of-day snapshot atomically.

    session_date: market data date in KST ISO string ("YYYY-MM-DD").
                  Defaults to trade_date if not supplied.
    is_final:     True when the market session is complete (time >= 15:30 KST).
                  Auto-detected from current KST time if not explicitly supplied.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    now_kst = datetime.now(KST)
    if session_date is None:
        session_date = trade_date
    if is_final is None:
        is_final = (now_kst.hour > _FINAL_CUTOFF_HOUR_KST or
                    (now_kst.hour == _FINAL_CUTOFF_HOUR_KST and
                     now_kst.minute >= _FINAL_CUTOFF_MINUTE_KST))

    data = {
        "trade_date":       trade_date,
        "session_date":     session_date,
        "generated_at":     now_kst.isoformat(),
        "is_final":         is_final,
        "trading_day":      trading_day,
        "regime":           regime,
        "kospi_pct":        kospi_pct,
        "ohlcv_by_symbol":  ohlcv_by_symbol,
        "prices":           {k: float(v) for k, v in prices.items()},
    }
    path = snapshot_path(base_dir, trade_date)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)
    logger.info(
        "EOD snapshot saved → %s (session=%s trading_day=%s is_final=%s)",
        path, session_date, trading_day, is_final,
    )
    return path


def load_snapshot(base_dir: Path, trade_date: str | None = None) -> dict:
    """Load the raw snapshot for trade_date (KST). Raises NoSnapshotError if absent."""
    trade_date = trade_date or _kst_today_iso()
    path = snapshot_path(base_dir, trade_date)
    if not path.exists():
        raise NoSnapshotError(
            f"No EOD snapshot for {trade_date} at {path}."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    # P0-5: all fields are required — no silent defaults for validation-critical keys.
    required = {
        "trade_date", "regime", "kospi_pct", "ohlcv_by_symbol", "prices",
        "session_date", "generated_at", "is_final", "trading_day",
    }
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Snapshot {path} is missing required keys: {missing}")
    return data


def load_and_validate_snapshot(base_dir: Path, trade_date: str | None = None) -> dict:
    """Load and validate a snapshot for EOD processing.

    Holiday policy (P0-4):
      When trading_day=False, session_date ≠ trade_date is expected (the provider
      returned prior-session data on a KRX holiday). This is accepted as long as
      is_final=True. Only a trading-day snapshot with session_date ≠ trade_date
      indicates an outage or stale file and is rejected.

    Raises:
      NoSnapshotError    — file absent (caller decides exit code based on day of week)
      StaleSnapshotError — trading-day snapshot with wrong session_date,
                           or any snapshot with is_final=False
      ValueError         — corrupt / missing required keys (programming error)
    """
    trade_date = trade_date or _kst_today_iso()
    data = load_snapshot(base_dir, trade_date)  # may raise NoSnapshotError or ValueError

    # P0-5: all fields required — KeyError here means the snapshot is corrupt.
    session_date = data["session_date"]
    is_holiday   = not data["trading_day"]

    if session_date != trade_date and not is_holiday:
        raise StaleSnapshotError(
            f"Snapshot session_date='{session_date}' ≠ trade_date='{trade_date}' "
            "on a trading day. Possible live-runner outage or stale file."
        )
    if session_date != trade_date and is_holiday:
        logger.info(
            "Holiday snapshot accepted: session_date=%s ≠ trade_date=%s "
            "(trading_day=False — KRX holiday, no session increment will occur)",
            session_date, trade_date,
        )

    if not data["is_final"]:
        raise StaleSnapshotError(
            f"Snapshot is_final=False for {trade_date}. "
            "Written before 15:30 KST — market may not be closed yet."
        )

    return data
