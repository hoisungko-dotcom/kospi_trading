from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kospi_bot_v2.config.settings import ShadowSettings
from kospi_bot_v2.domain.models import Trade
from kospi_bot_v2.market.data_provider import MarketDataProvider
from kospi_bot_v2.market.regime import RegimeClassifier
from kospi_bot_v2.reporting.daily_report import write_daily_report
from kospi_bot_v2.reporting.legacy_compare import append_comparison_section, summarize_legacy_log
from kospi_bot_v2.shadow.league import ShadowLeague
from kospi_bot_v2.strategy.signal_engine import SignalEngine
from runtime.live_runner import LiveRunResult

logger = logging.getLogger(__name__)


class ShadowRunner:
    """Shadow-only runtime path.

    This runner never calls broker sync or order APIs. It evaluates the market
    frame, updates the shadow league using observed prices, and writes the same
    report artifact shape expected by existing tooling.
    """

    def __init__(self, settings: ShadowSettings, provider: MarketDataProvider):
        self.settings = settings
        self.provider = provider
        self.regime_classifier = RegimeClassifier()
        self.signal_engine = SignalEngine(settings)
        self.league = ShadowLeague(Path(os.getenv("SHADOW_STATE_DIR", "data/shadow_league")))

    def run_post_close_snapshot_only(self) -> Path:
        from kospi_bot_v2.shadow.snapshot import save_snapshot

        kst = timezone(timedelta(hours=9))
        today_kst = datetime.now(kst).date()
        trade_date = today_kst.isoformat()

        frame = self.provider.load_universe_frame()
        snapshot = self.provider.market_snapshot(frame)
        regime = self.regime_classifier.classify(snapshot)

        latest = frame.sort_values("timestamp").groupby("symbol").tail(1)
        prices = {str(row["symbol"]): float(row["close"]) for _, row in latest.iterrows()}

        snap_ts = snapshot.timestamp
        if snap_ts.tzinfo is None:
            snap_ts = snap_ts.replace(tzinfo=kst)
        session_date_kst = snap_ts.astimezone(kst).date()
        trading_day = session_date_kst == today_kst

        ohlcv = {
            str(row["symbol"]): {
                "open": float(row.get("open", row["close"])),
                "high": float(row.get("high", row["close"])),
                "low": float(row.get("low", row["close"])),
                "close": float(row["close"]),
            }
            for _, row in latest.iterrows()
            if str(row.get("symbol", "")).strip()
        }

        path = save_snapshot(
            base_dir=self.league.base_dir,
            trade_date=trade_date,
            regime=regime.value,
            kospi_pct=snapshot.kospi_change_pct / 100,
            ohlcv_by_symbol=ohlcv,
            prices=prices,
            trading_day=trading_day,
            session_date=session_date_kst.isoformat(),
            is_final=True,
        )
        logger.info("📸 shadow post-close snapshot written: %s", path)
        return path

    def run_once(self) -> LiveRunResult:
        kst = timezone(timedelta(hours=9))
        now_kst = datetime.now(kst)
        today_kst = now_kst.date()
        trade_date = today_kst.isoformat()

        frame = self.provider.load_universe_frame()
        snapshot = self.provider.market_snapshot(frame)
        regime = self.regime_classifier.classify(snapshot)
        latest = frame.sort_values("timestamp").groupby("symbol").tail(1)
        prices = {str(row["symbol"]): float(row["close"]) for _, row in latest.iterrows()}

        snap_ts = snapshot.timestamp
        if snap_ts.tzinfo is None:
            snap_ts = snap_ts.replace(tzinfo=kst)
        session_date_kst = snap_ts.astimezone(kst).date()
        trading_day = session_date_kst == today_kst

        if trading_day:
            self.league.evaluate(latest, regime.value, prices, snapshot.timestamp, trade_date)
        else:
            logger.info(
                "shadow evaluate skipped — non-trading day (session_date=%s today_kst=%s)",
                session_date_kst,
                today_kst,
            )

        signals = self.signal_engine.generate(frame, regime)
        shadow_nav = sum(port.nav(prices) for port in self.league.portfolios)
        shadow_trades: list[Trade] = []

        report_path = write_daily_report(
            self.settings.report_dir,
            datetime.now(),
            regime,
            signals,
            shadow_trades,
            shadow_nav,
            None,
        )
        legacy = summarize_legacy_log(self.settings.compare_log_path)
        append_comparison_section(report_path, legacy, signals, shadow_trades)

        return LiveRunResult(
            regime=regime,
            signals=signals,
            exits=[],
            report_path=str(report_path),
            equity=shadow_nav,
            cash=shadow_nav,
            n_positions=sum(len(port.positions) for port in self.league.portfolios),
        )
