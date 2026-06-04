"""
ShadowLeague — orchestrates all shadow portfolios.

Call sites:
  - Live runner intraday loop:
      league.evaluate(frame, regime, prices, timestamp)    ← entry + intraday exit
  - Live runner intraday loop (exit-only pass):
      league.evaluate_exits(prices, timestamp)
  - kr-shadow-daily.service at 16:00:
      league.finalize_day(ohlcv_by_symbol, trade_date, regime, kospi_pct)
  - kr-shadow-weekly.service on Friday:
      league.weekly_report(...)

Key invariants:
  - Each strategy evaluates the same raw frame row independently.
  - B/C/D never receive pre-filtered signals from A.
  - Candidate ordering is deterministic: score desc → volume_ratio desc → symbol asc.
  - Shadow league never calls live broker order methods.
  - finalize_day is idempotent: repeated calls for the same date are no-ops.
  - KSPI/KDQ proxy rows are always excluded before strategy evaluation (P0-4).

NOTE on universe (P0-5): In live mode, all strategies (A–E) receive the same candidate
pool pre-selected by the legacy top_10_daily.json screener upstream of this module.
This experiment is an entry/exit-rule comparison within the screened pool, NOT a
comparison across independent broad universes.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
import pandas as pd

from kospi_bot_v2.shadow.portfolio import ShadowPortfolio
from kospi_bot_v2.shadow.strategies.definitions import ALL_STRATEGIES, BaseShadowStrategy
from kospi_bot_v2.shadow.reporter import build_daily_report, build_weekly_report, save_and_notify

logger = logging.getLogger(__name__)

INITIAL_CAPITAL  = 2_000_000   # ₩2M per strategy — identical for all
MAX_POSITIONS    = 2
MAX_DAILY_ENTRY  = 1            # max 1 new shadow entry per strategy per day
SMA224_MIN_ROWS  = 260          # D-v1 requires this many rows of history

# P0-4: non-tradable market index proxy symbols — filtered before strategy evaluation
_PROXY_PREFIXES = ("KSPI", "KDQ")


class ShadowLeague:
    """Parallel shadow portfolios for all registered strategies.

    State directory layout:
      data/shadow_league/
        state_{id}_{version}.json   ← per-strategy position/cash state (atomic writes)
        meta.json                   ← last_finalized_date (idempotency guard)
        eod_snapshot_{date}.json    ← daily market snapshot written by live runner
        daily/YYYY-MM-DD.txt
        weekly/{label}.txt
        latest.txt                  ← always the most recent daily summary
    """

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "daily").mkdir(exist_ok=True)
        (base_dir / "weekly").mkdir(exist_ok=True)

        # P1-5: D-v1 is inactive until the live data pipeline provides valid SMA224
        # values with at least SMA224_MIN_ROWS of history. It will self-skip gracefully,
        # but expect zero entries from D-v1 until the data pipeline is confirmed.
        self.portfolios: list[ShadowPortfolio] = [
            ShadowPortfolio(
                strategy=s,
                initial_capital=INITIAL_CAPITAL,
                max_positions=MAX_POSITIONS,
                max_daily_entries=MAX_DAILY_ENTRY,
                state_path=base_dir / f"state_{s.strategy_id}_{s.version}.json",  # P1-2
            )
            for s in ALL_STRATEGIES
        ]

        # Idempotency: track the last date that was fully finalized (P0-4)
        self._meta_path = base_dir / "meta.json"
        meta = json.loads(self._meta_path.read_text()) if self._meta_path.exists() else {}
        self._last_finalized_date: str = meta.get("last_finalized_date", "")

        # Data-quality counters (reset each day by finalize_day)
        self._loops_today:       int = 0
        self._missing_prices:    int = 0
        self._skipped_no_ind:    int = 0
        self._eligible_counts:   dict[str, int] = {}   # per-strategy, per-day
        self._errors_today:      list[str] = []
        self._today_str:         str = ""

    # ── Core evaluation: called every intraday loop ─────────────────────────

    def evaluate(
        self,
        frame: pd.DataFrame,
        regime: str,
        prices: dict[str, float],
        timestamp: datetime,
        trade_date: str | None = None,
    ) -> None:
        """Evaluate all strategies against the same raw market frame.

        Each strategy independently tests every row. This is the ONLY entry path
        for shadow entries — strategies never share filtered signals.

        frame: DataFrame where each row is a candidate symbol with all indicators.
               Must include columns: symbol, close, sma20, sma60, high20,
               volume, avg_volume20, rsi14, return5, return20, volume_ratio, etc.
        regime: market regime string ("BULL" / "NEUTRAL" / "WEAK" / "CRASH")
        prices: {symbol: current_price} for intraday mark and exit checks
        timestamp: evaluation moment (used for logging and state)
        trade_date: ISO date string; defaults to today
        """
        trade_date = trade_date or date.today().isoformat()
        self._reset_day_if_new(trade_date)
        self._loops_today += 1

        # Count missing prices for data-quality report
        for _, row in frame.iterrows():
            sym = str(row.get("symbol", ""))
            if sym and sym not in prices:
                self._missing_prices += 1

        # Count skipped rows due to missing key indicators
        required = ["close", "sma20", "sma60", "rsi14", "return5", "volume_ratio"]
        ok_rows = frame[frame[required].notna().all(axis=1)]
        # P0-4: exclude non-tradable market index proxies from candidate pool
        ok_rows = ok_rows[~ok_rows["symbol"].astype(str).str.startswith(_PROXY_PREFIXES)]
        skipped = len(frame) - len(ok_rows)
        self._skipped_no_ind += skipped
        if skipped > 0:
            logger.debug("Skipped %d rows with missing indicators", skipped)

        # Check D-v1 SMA224 availability and log if insufficient
        if "sma224" in frame.columns:
            missing_sma224 = ok_rows["sma224"].isna().sum() + (ok_rows["sma224"] == 0).sum()
            if missing_sma224 > 0 and missing_sma224 == len(ok_rows):
                msg = (f"D-v1: SMA224 unavailable for all {len(ok_rows)} candidates "
                       f"— need {SMA224_MIN_ROWS}+ rows of history")
                logger.warning(msg)
                self._errors_today.append(msg)

        # Sort candidates deterministically: score desc → volume_ratio desc → symbol asc
        for portfolio in self.portfolios:
            sid = portfolio.strategy.strategy_id
            self._eligible_counts.setdefault(sid, 0)

            candidates = _rank_candidates(ok_rows, portfolio.strategy, regime)
            self._eligible_counts[sid] += len(candidates)

            for row in candidates:
                sym = str(row.get("symbol", ""))
                # P0-4: no stale fallback — only enter with a confirmed live price
                price = prices.get(sym)
                if price is None or price <= 0:
                    continue
                trade = portfolio.try_enter(row, regime, trade_date, float(price))
                if trade is not None:
                    break  # daily entry limit: one entry per strategy per day

        # Also check exits against current intraday prices
        self.evaluate_exits(prices, timestamp, trade_date)

    def evaluate_exits(
        self,
        prices: dict[str, float],
        _timestamp: datetime,
        trade_date: str | None = None,
    ) -> None:
        """Check all open shadow positions against current prices.

        Called on every intraday loop (not only at 16:00).
        Uses only current price (no full OHLCV bar) — conservative: stop/take only.
        """
        trade_date = trade_date or date.today().isoformat()
        for portfolio in self.portfolios:
            portfolio.evaluate_intraday_exits(prices, trade_date)

    # ── End-of-day finalization ──────────────────────────────────────────────

    def finalize_day(
        self,
        ohlcv_by_symbol: dict[str, dict],
        trade_date: str,
        regime: str,
        kospi_pct: float,
        current_prices: dict[str, float] | None = None,
        trading_day: bool = True,
        send_telegram: bool = True,
    ) -> str:
        """Mark all positions with full OHLCV bar, generate daily report.

        P0-4: Idempotent — repeated calls for the same trade_date are a no-op that
        returns the cached report text without re-running exits or incrementing sessions.

        trading_day: False on KRX holidays — reports but does not count as a trading day.
        ohlcv_by_symbol: {symbol: {"open":…, "high":…, "low":…, "close":…}}
        """
        # P0-4: idempotency guard — same date must not process exits twice
        if trade_date == self._last_finalized_date:
            logger.info(
                "finalize_day(%s) already completed — returning cached report", trade_date
            )
            cached = self.base_dir / "daily" / f"{trade_date}.txt"
            return cached.read_text(encoding="utf-8") if cached.exists() else ""

        current_prices = current_prices or {}
        entries_today: dict[str, list] = {}
        exits_today:   dict[str, list] = {}

        for portfolio in self.portfolios:
            sid = portfolio.strategy.strategy_id
            entries_today[sid] = portfolio.get_entries_today(trade_date)
            if trading_day:
                portfolio.mark_day(ohlcv_by_symbol, trade_date)
                # P0-2: record NAV snapshot for equity-curve PnL/MDD calculation
                portfolio.record_eod_nav(trade_date, current_prices)
            else:
                logger.info("[%s] KRX holiday %s — mark_day skipped", sid, trade_date)
            # P1-2: report ALL exits today (intraday + mark_day)
            exits_today[sid] = portfolio.get_exits_today(trade_date)

        dq = _DataQuality(
            loops=self._loops_today,
            missing_prices=self._missing_prices,
            skipped_no_ind=self._skipped_no_ind,
            eligible_counts=dict(self._eligible_counts),
        )

        report = build_daily_report(
            portfolios=self.portfolios,
            regime=regime,
            kospi_pct=kospi_pct,
            as_of=date.fromisoformat(trade_date),
            current_prices=current_prices,
            entries_today=entries_today,
            exits_today=exits_today,
            errors=self._errors_today,
            data_quality=dq,
            trading_day=trading_day,
        )

        out = self.base_dir / "daily" / f"{trade_date}.txt"
        save_and_notify(report, out, telegram=send_telegram)
        (self.base_dir / "latest.txt").write_text(report, encoding="utf-8")

        # Persist last finalized date for idempotency (P1-2: atomic write)
        self._last_finalized_date = trade_date
        _meta_tmp = self._meta_path.with_suffix(".tmp")
        _meta_tmp.write_text(
            json.dumps({"last_finalized_date": trade_date}), encoding="utf-8"
        )
        _meta_tmp.replace(self._meta_path)

        self._reset_day_if_new("")  # clear counters after finalization
        return report

    # ── Weekly report ────────────────────────────────────────────────────────

    def weekly_report(
        self,
        week_label: str,
        period_str: str,
        trading_days_elapsed: int,
        next_eval_date: str,
        current_prices: dict[str, float] | None = None,
        send_telegram: bool = True,
    ) -> str:
        report = build_weekly_report(
            portfolios=self.portfolios,
            week_label=week_label,
            period_str=period_str,
            trading_days_elapsed=trading_days_elapsed,
            next_eval_date=next_eval_date,
            current_prices=current_prices or {},
        )
        slug = week_label.replace(" ", "_")
        out = self.base_dir / "weekly" / f"{slug}.txt"
        save_and_notify(report, out, telegram=send_telegram)
        return report

    # ── Live runner helpers ──────────────────────────────────────────────────

    def dump_eod_snapshot(
        self,
        trade_date: str,
        regime: str,
        kospi_pct: float,
        ohlcv_by_symbol: dict,
        prices: dict[str, float],
        trading_day: bool = True,
    ) -> None:
        """Persist today's market snapshot for the daily_finalize oneshot service.

        Called at the end of each live_runner.run_once() so the daily script
        can finalize positions without re-fetching market data.
        """
        from kospi_bot_v2.shadow.snapshot import save_snapshot
        save_snapshot(
            base_dir=self.base_dir,
            trade_date=trade_date,
            regime=regime,
            kospi_pct=kospi_pct,
            ohlcv_by_symbol=ohlcv_by_symbol,
            prices=prices,
            trading_day=trading_day,
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _reset_day_if_new(self, trade_date: str) -> None:
        if trade_date != self._today_str:
            self._loops_today     = 0
            self._missing_prices  = 0
            self._skipped_no_ind  = 0
            self._eligible_counts = {}
            self._errors_today    = []
            self._today_str       = trade_date

    def status_summary(self) -> dict:
        return {
            p.strategy.strategy_id: {
                "version":       p.strategy.version,
                "nav":           p.nav(),
                "cash":          p.cash,
                "open_positions": len(p.positions),
                "closed_trades":  len(p.closed_trades),
            }
            for p in self.portfolios
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

class _DataQuality:
    __slots__ = ("loops", "missing_prices", "skipped_no_ind", "eligible_counts")

    def __init__(self, loops, missing_prices, skipped_no_ind, eligible_counts):
        self.loops           = loops
        self.missing_prices  = missing_prices
        self.skipped_no_ind  = skipped_no_ind
        self.eligible_counts = eligible_counts


def _rank_candidates(
    frame: pd.DataFrame,
    strategy: BaseShadowStrategy,
    regime: str,
) -> list[pd.Series]:
    """Filter and sort frame rows that this strategy wants to enter.

    Deterministic ordering:
      1. strategy score descending
      2. volume_ratio descending
      3. symbol ascending (final tie-break)
    """
    qualifying = []
    for _, row in frame.iterrows():
        if strategy.should_enter(row, regime):
            qualifying.append((
                -strategy.score(row),                    # negative for desc sort
                -float(row.get("volume_ratio", 0)),
                str(row.get("symbol", "")),
                row,
            ))
    qualifying.sort(key=lambda t: (t[0], t[1], t[2]))
    return [item[3] for item in qualifying]
