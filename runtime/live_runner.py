from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone  # date used for type annotation
import os
from pathlib import Path

from kospi_bot_v2.config.settings import ShadowSettings
from kospi_bot_v2.shadow.runner_integration import shadow_evaluate
from kospi_bot_v2.domain.models import MarketRegime, Signal, Trade
from kospi_bot_v2.market.data_provider import MarketDataProvider
from kospi_bot_v2.market.regime import RegimeClassifier
from kospi_bot_v2.portfolio.live_broker import BrokerLiveAdapter
from kospi_bot_v2.reporting.daily_report import write_daily_report
from kospi_bot_v2.reporting.legacy_compare import append_comparison_section, summarize_legacy_log
from kospi_bot_v2.risk.position_sizer import PositionSizer
from kospi_bot_v2.risk.risk_guard import RiskGuard
from kospi_bot_v2.strategy.signal_engine import SignalEngine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveRunResult:
    regime: MarketRegime
    signals: list[Signal]
    exits: list[Trade]
    report_path: str
    equity: float
    cash: float
    n_positions: int


class LiveRunner:
    def __init__(self, settings: ShadowSettings, provider: MarketDataProvider):
        self.settings = settings
        self.provider = provider
        self.regime_classifier = RegimeClassifier()
        self.signal_engine = SignalEngine(settings)
        self.sizer = PositionSizer(settings)
        self.guard = RiskGuard(settings)
        self.broker = BrokerLiveAdapter(settings)
        self._day_start_date: date | None = None
        self._day_start_equity: float | None = None

    def _daily_pnl_pct(self, equity: float) -> float:
        # P0-3: use KST date so the intraday-high-water mark resets at KST midnight
        today = datetime.now(timezone(timedelta(hours=9))).date()
        if self._day_start_date != today or not self._day_start_equity:
            self._day_start_date = today
            self._day_start_equity = equity
            return 0.0
        return (equity / max(self._day_start_equity, 1)) - 1.0

    def _recent_sell_cooldowns(self) -> dict[str, datetime]:
        cooldown = timedelta(minutes=self.settings.reentry_cooldown_minutes)
        if cooldown.total_seconds() <= 0:
            return {}
        path = self.settings.ledger_path
        if not path.exists():
            return {}
        now = datetime.now()
        blocked: dict[str, datetime] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-200:]
        except OSError as exc:
            logger.warning("⚠️ ledger cooldown read failed: %s", exc)
            return {}
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("action") != "SELL":
                continue
            pnl_pct = event.get("pnl_pct")
            if pnl_pct is None or float(pnl_pct) >= 0:
                continue
            raw_ts = event.get("recorded_at") or event.get("timestamp")
            if not raw_ts:
                continue
            try:
                sold_at = datetime.fromisoformat(str(raw_ts))
            except ValueError:
                continue
            expires_at = sold_at + cooldown
            if expires_at > now:
                blocked[str(event.get("symbol"))] = expires_at
        return blocked

    def _minute_entry_confirm(self, signal: Signal) -> tuple[bool, str]:
        if os.getenv("V2_MINUTE_CONFIRM_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
            return True, "disabled"
        try:
            minute_df = self.broker.client.get_intraday_ohlcv(signal.symbol, interval="1m", lookback=10)
        except Exception as exc:
            return False, f"1분봉 조회 실패: {exc}"
        if minute_df is None or len(minute_df) < 5:
            return False, "1분봉 데이터 부족"

        recent = minute_df.copy()
        if "time" in recent.columns:
            recent = recent.sort_values(["date", "time"] if "date" in recent.columns else ["time"])
        recent = recent.tail(10)
        close = recent["close"].astype(float)
        high = recent["high"].astype(float)
        volume = recent["volume"].astype(float)
        if len(close) < 5:
            return False, "1분봉 유효 데이터 부족"

        last = float(close.iloc[-1])
        diffs = close.diff().tail(3)
        down_count = int((diffs < 0).sum())
        up_count = int((diffs > 0).sum())
        recent_high = float(high.tail(8).max())
        pullback_pct = ((recent_high - last) / recent_high) if recent_high else 0.0

        if down_count >= 3:
            return False, f"1분봉 하락전환({down_count}/3봉)"
        if pullback_pct >= float(os.getenv("V2_MINUTE_MAX_PULLBACK_PCT", "0.012") or 0.012):
            return False, f"1분봉 고점대비 밀림({pullback_pct * 100:.1f}%)"
        recent_vol = float(volume.iloc[-1] + volume.iloc[-2])
        prev_vol = float(volume.iloc[-3] + volume.iloc[-4])
        if prev_vol > 0 and recent_vol < prev_vol * 0.4:
            return False, "1분봉 거래량 소멸"

        return (
            True,
            f"1분봉 확인(1m last={last:.0f}, up={up_count}, "
            f"down={down_count}, pullback={pullback_pct * 100:.1f}%)",
        )

    def run_post_close_snapshot_only(self) -> Path:
        """Fetch market data and write the final EOD snapshot. No broker operations.

        Called exactly once after the active window closes (≥15:30 KST).
        Does NOT call broker.sync(), evaluate_exits(), buy(), or sell().
        """
        from kospi_bot_v2.shadow.snapshot import save_snapshot

        _KST = timezone(timedelta(hours=9))
        _today_kst = datetime.now(_KST).date()
        trade_date = _today_kst.isoformat()

        frame = self.provider.load_universe_frame()
        snapshot = self.provider.market_snapshot(frame)
        regime = self.regime_classifier.classify(snapshot)

        latest = frame.sort_values("timestamp").groupby("symbol").tail(1)
        prices = {str(row["symbol"]): float(row["close"]) for _, row in latest.iterrows()}

        _snap_ts = snapshot.timestamp
        if _snap_ts.tzinfo is None:
            _snap_ts = _snap_ts.replace(tzinfo=_KST)
        _session_date_kst = _snap_ts.astimezone(_KST).date()
        _trading_day = (_session_date_kst == _today_kst)

        _ohlcv = {
            str(row["symbol"]): {
                "open":  float(row.get("open",  row["close"])),
                "high":  float(row.get("high",  row["close"])),
                "low":   float(row.get("low",   row["close"])),
                "close": float(row["close"]),
            }
            for _, row in latest.iterrows()
            if str(row.get("symbol", "")).strip()
        }

        path = save_snapshot(
            base_dir=Path(os.getenv("SHADOW_STATE_DIR", "data/shadow_league")),
            trade_date=trade_date,
            regime=regime.value,
            kospi_pct=snapshot.kospi_change_pct / 100,
            ohlcv_by_symbol=_ohlcv,
            prices=prices,
            trading_day=_trading_day,
            session_date=_session_date_kst.isoformat(),
            is_final=True,  # always final — only called post-close
        )
        logger.info("📸 post-close snapshot written (no broker ops): %s", path)
        return path

    def run_once(self) -> LiveRunResult:
        # P0-3: one canonical KST clock for this entire run_once() call.
        # trade_date drives snapshot paths and shadow-portfolio daily state.
        _KST = timezone(timedelta(hours=9))
        _now_kst = datetime.now(_KST)
        _today_kst = _now_kst.date()
        trade_date = _today_kst.isoformat()

        frame = self.provider.load_universe_frame()
        snapshot = self.provider.market_snapshot(frame)
        regime = self.regime_classifier.classify(snapshot)
        logger.info(
            "📊 KOSPI v4.3 live regime=%s kospi=%+.2f%% kosdaq=%+.2f%% advance=%.0f%% vol=%.2f%%",
            regime.value,
            snapshot.kospi_change_pct,
            snapshot.kosdaq_change_pct,
            snapshot.advance_ratio * 100,
            snapshot.volatility_pct,
        )

        latest = frame.sort_values("timestamp").groupby("symbol").tail(1)
        prices = {str(row["symbol"]): float(row["close"]) for _, row in latest.iterrows()}

        # P0-2: compute _trading_day BEFORE any broker order operations.
        # Naive timestamps from KISQuoteOnlyProvider represent KST (P1-1 follow-up).
        _snap_ts = snapshot.timestamp
        if _snap_ts.tzinfo is None:
            _snap_ts = _snap_ts.replace(tzinfo=_KST)  # naive → KST
        _session_date_kst = _snap_ts.astimezone(_KST).date()
        _trading_day = (_session_date_kst == _today_kst)

        self.broker.sync()

        exits: list[Trade] = []
        if not _trading_day:
            logger.info(
                "broker exits/entries skipped — non-trading day "
                "(session_date=%s ≠ today_kst=%s: KRX holiday or stale data)",
                _session_date_kst, _today_kst,
            )
        else:
            missing_position_symbols = [
                symbol for symbol in self.broker.positions
                if symbol not in prices
            ]
            if missing_position_symbols:
                try:
                    live_prices = self.broker.client.get_current_prices(missing_position_symbols)
                except Exception as exc:
                    live_prices = {}
                    logger.warning(
                        "⚠️ held position live price lookup failed for %s: %s",
                        missing_position_symbols,
                        exc,
                    )
                for symbol, price in live_prices.items():
                    if price:
                        prices[symbol] = float(price)
                logger.info(
                    "💹 held position prices refreshed: requested=%s received=%s",
                    missing_position_symbols,
                    sorted(live_prices.keys()),
                )
            exits = self.broker.evaluate_exits(prices, snapshot.timestamp)

        # P0-2: skip shadow evaluation on non-trading days (holiday / stale provider data)
        if _trading_day:
            shadow_evaluate(latest, regime.value, prices, snapshot.timestamp, trade_date)
        else:
            logger.info(
                "shadow_evaluate skipped — non-trading day "
                "(session_date=%s ≠ today_kst=%s: KRX holiday or stale data)",
                _session_date_kst, _today_kst,
            )

        signals = self.signal_engine.generate(frame, regime)
        diagnostics = self.signal_engine.diagnose(frame, regime)
        if diagnostics:
            status_counts = Counter(str(item["status"]) for item in diagnostics)
            reject_counts = Counter(
                str(item["reject"]) for item in diagnostics if item.get("reject")
            )
            logger.info(
                "🧪 signal diagnostics: total=%d status=%s rejects=%s",
                len(diagnostics),
                dict(status_counts),
                dict(reject_counts.most_common(5)),
            )
            for item in diagnostics[:10]:
                logger.info(
                    "🧪 candidate %s %s status=%s reject=%s strategy=%s score=%s grade=%s "
                    "r5=%+.1f%% r20=%+.1f%% rsi=%.1f vol/avg=%.2f atr=%.1f%%",
                    item["symbol"],
                    item["name"],
                    item["status"],
                    item["reject"] or "-",
                    item["strategy"] or "-",
                    "-" if item["score"] is None else item["score"],
                    item["grade"] or "-",
                    item["return5"] * 100,
                    item["return20"] * 100,
                    item["rsi14"],
                    item["volume_ratio"],
                    item["atr_pct"] * 100,
                )
        equity = self.broker.equity(prices)
        daily_pnl_pct = self._daily_pnl_pct(equity)

        executed: list[Signal] = []
        if not _trading_day:
            logger.info("— live new buy 없음 (non-trading day)")
        else:
            cooldowns = self._recent_sell_cooldowns()
            for signal in signals:
                signal.metadata["evaluated_at"] = snapshot.timestamp
                if signal.symbol in self.broker.positions:
                    continue
                if signal.symbol in cooldowns:
                    logger.info(
                        "⏳ %s entry skipped: loss re-entry cooldown until %s",
                        signal.symbol,
                        cooldowns[signal.symbol].strftime("%H:%M:%S"),
                    )
                    continue
                minute_ok, minute_reason = self._minute_entry_confirm(signal)
                if not minute_ok:
                    logger.info("⏸️ %s entry deferred: %s", signal.symbol, minute_reason)
                    continue
                logger.info("✅ %s entry minute confirm: %s", signal.symbol, minute_reason)
                allowed, deny_reason = self.guard.allow_new_entry(regime, self.broker.positions, daily_pnl_pct)
                if not allowed:
                    logger.info("⛔ %s entry blocked: %s", signal.symbol, deny_reason)
                    break
                quantity = self.sizer.quantity(self.broker.cash, self.broker.equity(prices), signal, regime)
                trade = self.broker.buy(signal, quantity)
                if trade:
                    executed.append(signal)

            if executed:
                logger.warning("✅ live buys: %s", [s.symbol for s in executed])
            else:
                logger.info("— live new buy 없음")

        equity = self.broker.equity(prices)
        report_path = write_daily_report(
            self.settings.report_dir,
            datetime.now(),
            regime,
            signals,
            self.broker.trades,
            equity,
            None,
        )
        legacy = summarize_legacy_log(self.settings.compare_log_path)
        append_comparison_section(report_path, legacy, signals, self.broker.trades)

        # Persist EOD snapshot for kr-shadow-daily.service
        try:
            from kospi_bot_v2.shadow.snapshot import save_snapshot
            _ohlcv = {
                str(row["symbol"]): {
                    "open":  float(row.get("open",  row["close"])),
                    "high":  float(row.get("high",  row["close"])),
                    "low":   float(row.get("low",   row["close"])),
                    "close": float(row["close"]),
                }
                for _, row in latest.iterrows()
                if str(row.get("symbol", "")).strip()
            }
            # Use values already computed at the top of run_once() (P0-1/P0-2/P0-3).
            # P1-1: recompute from actual wall time — run_once() may take minutes to execute.
            _save_kst = datetime.now(_KST)
            _is_final = (_save_kst.hour > 15 or
                         (_save_kst.hour == 15 and _save_kst.minute >= 30))
            save_snapshot(
                base_dir=Path(os.getenv("SHADOW_STATE_DIR", "data/shadow_league")),
                trade_date=trade_date,
                regime=regime.value,
                kospi_pct=snapshot.kospi_change_pct / 100,  # stored as fraction
                ohlcv_by_symbol=_ohlcv,
                prices=prices,
                trading_day=_trading_day,
                session_date=_session_date_kst.isoformat(),
                is_final=_is_final,
            )
        except Exception as _snap_exc:
            logger.warning("EOD snapshot dump failed (non-fatal): %s", _snap_exc)

        return LiveRunResult(
            regime=regime,
            signals=signals,
            exits=exits,
            report_path=str(report_path),
            equity=equity,
            cash=self.broker.cash,
            n_positions=len(self.broker.positions),
        )
