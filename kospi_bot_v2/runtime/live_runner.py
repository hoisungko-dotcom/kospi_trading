from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os

from kospi_bot_v2.config.settings import ShadowSettings
from kospi_bot_v2.domain.models import MarketRegime, Signal, Trade
from kospi_bot_v2.market.data_provider import MarketDataProvider
from kospi_bot_v2.market.regime import RegimeClassifier
from kospi_bot_v2.portfolio.live_broker import KISLiveBroker
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
        self.broker = KISLiveBroker(settings)
        self._day_start_date: date | None = None
        self._day_start_equity: float | None = None

    def _daily_pnl_pct(self, equity: float) -> float:
        today = date.today()
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
        prev = float(close.iloc[-2])
        diffs = close.diff().tail(3)
        down_count = int((diffs < 0).sum())
        up_count = int((diffs > 0).sum())
        recent_high = float(high.tail(8).max())
        pullback_pct = ((recent_high - last) / recent_high) if recent_high else 0.0

        if down_count >= 2:
            return False, f"1분봉 하락전환({down_count}/3봉)"
        if pullback_pct >= float(os.getenv("V2_MINUTE_MAX_PULLBACK_PCT", "0.012") or 0.012):
            return False, f"1분봉 고점대비 밀림({pullback_pct * 100:.1f}%)"
        recent_vol = float(volume.iloc[-1] + volume.iloc[-2])
        prev_vol = float(volume.iloc[-3] + volume.iloc[-4])
        if prev_vol > 0 and recent_vol < prev_vol * 0.4:
            return False, "1분봉 거래량 소멸"
        if last <= prev and up_count == 0:
            return False, "1분봉 반등 미확인"

        try:
            five_df = self.broker.client.get_intraday_ohlcv(signal.symbol, interval="5m", lookback=8)
        except Exception as exc:
            return False, f"5분봉 조회 실패: {exc}"
        if five_df is None or len(five_df) < 4:
            return False, "5분봉 데이터 부족"
        five = five_df.copy()
        if "time" in five.columns:
            five = five.sort_values(["date", "time"] if "date" in five.columns else ["time"])
        five_close = five["close"].astype(float).tail(6)
        five_open = five["open"].astype(float).tail(6)
        five_down = int((five_close.diff().tail(3) < 0).sum())
        if five_down >= 2:
            return False, f"5분봉 하락전환({five_down}/3봉)"
        if float(five_close.iloc[-1]) < float(five_open.iloc[-1]):
            return False, "5분봉 음봉 진행"

        flow_reason = "수급 데이터 없음"
        try:
            flow = self.broker.client.get_foreign_net_buying(signal.symbol, lookback=5)
        except Exception as exc:
            flow = []
            flow_reason = f"수급 조회 실패: {exc}"
        if flow:
            recent_flow = [float(item.get("foreigner_net", 0) or 0) for item in flow[-3:]]
            if len(recent_flow) >= 3 and all(value <= 0 for value in recent_flow):
                return False, "외국계 3일 연속 순매도"
            flow_reason = f"외국계 최근3일 합계 {sum(recent_flow):+.0f}"

        return (
            True,
            f"1분봉/5분봉/수급 확인(1m last={last:.0f}, up={up_count}, "
            f"pullback={pullback_pct * 100:.1f}%, 5m_down={five_down}, {flow_reason})",
        )

    def run_once(self) -> LiveRunResult:
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

        self.broker.sync()
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

        return LiveRunResult(
            regime=regime,
            signals=signals,
            exits=exits,
            report_path=str(report_path),
            equity=equity,
            cash=self.broker.cash,
            n_positions=len(self.broker.positions),
        )
