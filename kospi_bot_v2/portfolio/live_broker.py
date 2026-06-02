from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from kospi_bot_v2.config.settings import ShadowSettings
from kospi_bot_v2.domain.models import Position, Signal, SignalAction, StrategyType, Trade

logger = logging.getLogger(__name__)


class KISLiveBroker:
    """KIS-backed live broker for the v4.3 engine.

    The strategy and risk engine stay in kospi_bot_v2; this adapter only
    converts accepted signals into real KIS domestic stock orders and rebuilds
    in-memory positions from the account balance after each order.
    """

    def __init__(self, settings: ShadowSettings):
        self.settings = settings
        self.settings.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        from core.kis_client_kospi import KISClientKospi

        self.client = KISClientKospi()
        self.positions: dict[str, Position] = {}
        self.cash: float = 0.0
        self.trades: list[Trade] = []
        self._sync_fail_count: int = 0
        self._last_alert_t: float = 0.0
        self._last_sync_t: float = 0.0
        self._sync_min_interval_sec: float = float(os.getenv("V2_BALANCE_SYNC_MIN_INTERVAL_SEC", "90") or 90)
        self.sync(force=True)

    def _alert(self, text: str) -> None:
        """30분 쿨다운으로 텔레그램 알림."""
        now = time.time()
        if now - self._last_alert_t < 1800:
            return
        self._last_alert_t = now
        try:
            from kospi_bot_v2.notifications import send_telegram
            send_telegram(text)
        except Exception as e:
            logger.warning("알림 전송 실패: %s", e)

    def sync(self, force: bool = False) -> None:
        now = time.time()
        has_cached_state = bool(self.positions) or self.cash > 0
        if not force and has_cached_state and now - self._last_sync_t < self._sync_min_interval_sec:
            logger.info(
                "↻ KIS live sync reused cached account state: cash ₩%.0f, positions %d, age %.1fs",
                self.cash,
                len(self.positions),
                now - self._last_sync_t,
            )
            return
        self._last_sync_t = now
        raw = self.client.get_balance()
        if not raw:
            self._sync_fail_count += 1
            if self._sync_fail_count >= 3 and not has_cached_state:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                msg = (
                    f"❌ 한국봇 v4.3 KIS 잔고조회 {self._sync_fail_count}회 연속 실패\n"
                    f"캐시된 계좌 상태 없음 — 즉시 확인 필요\n"
                    f"시각: {ts}"
                )
                self._alert(msg)
            elif self._sync_fail_count >= 3:
                logger.warning(
                    "⚠️ KIS live sync failed %d times, but cached account state is preserved",
                    self._sync_fail_count,
                )
            if not self.positions and self.cash <= 0:
                logger.warning(
                    "⚠️ KIS live sync skipped: balance unavailable, no cached account state yet"
                )
            else:
                logger.warning(
                    "⚠️ KIS live sync skipped: balance unavailable, keeping cached cash ₩%.0f and positions %d",
                    self.cash,
                    len(self.positions),
                )
            return
        else:
            self._sync_fail_count = 0
        balance = raw or {}
        self.cash = float(balance.get("cash", 0) or 0)
        holdings = balance.get("holdings", {}) or {}
        rebuilt: dict[str, Position] = {}
        for symbol, info in holdings.items():
            qty = int(float(info.get("quantity", 0) or 0))
            if qty <= 0:
                continue
            entry_price = float(info.get("price", 0) or 0)
            current_price = float(info.get("highest_price", 0) or entry_price)
            if entry_price <= 0:
                entry_price = current_price
            rebuilt[symbol] = Position(
                symbol=symbol,
                name=symbol,
                strategy=StrategyType.MOMENTUM,
                entry_time=None,
                entry_price=entry_price,
                quantity=qty,
                peak_price=max(entry_price, current_price),
                stop_price=entry_price * (1 + self.settings.stop_loss_pct),
                metadata={"source": "kis_balance"},
            )
        self.positions = rebuilt
        logger.info("✅ KIS live sync: cash ₩%.0f, positions %d", self.cash, len(self.positions))

    def equity(self, prices: dict[str, float] | None = None) -> float:
        prices = prices or {}
        value = self.cash
        for symbol, position in self.positions.items():
            value += position.market_value(prices.get(symbol, position.entry_price))
        return value

    def buy(self, signal: Signal, quantity: int) -> Trade | None:
        if quantity <= 0 or signal.symbol in self.positions:
            return None
        orderable = self.client.get_orderable_cash(signal.symbol, signal.price, use_max=True)
        if orderable >= 0 and signal.price * quantity > orderable:
            quantity = int((orderable * 0.99) // signal.price)
        if quantity <= 0:
            logger.info("⏭ %s live buy skipped: no orderable cash", signal.symbol)
            return None

        previous_qty = self.positions.get(signal.symbol).quantity if signal.symbol in self.positions else 0
        ok = self.client.place_buy_order(
            signal.symbol,
            quantity,
            signal.price,
            allow_price_chase=signal.score >= 90,
        )
        if not ok:
            return None

        if not self.client.verify_domestic_fill(signal.symbol, "BUY", previous_qty, quantity):
            self.sync(force=True)
            return None

        trade = Trade(
            timestamp=signal.metadata.get("evaluated_at"),
            symbol=signal.symbol,
            name=signal.name,
            action=SignalAction.BUY,
            strategy=signal.strategy,
            price=signal.price,
            quantity=quantity,
            pnl_pct=None,
            reason=signal.reason,
        )
        self._record(trade)
        self.sync(force=True)
        return trade

    def sell(self, symbol: str, price: float, reason: str, timestamp) -> Trade | None:
        position = self.positions.get(symbol)
        if position is None or position.quantity <= 0:
            return None
        previous_qty = position.quantity
        ok = self.client.place_sell_order(symbol, previous_qty, price)
        if not ok:
            return None
        if not self.client.verify_domestic_fill(symbol, "SELL", previous_qty, previous_qty):
            self.sync(force=True)
            return None

        trade = Trade(
            timestamp=timestamp,
            symbol=position.symbol,
            name=position.name,
            action=SignalAction.SELL,
            strategy=position.strategy,
            price=price,
            quantity=previous_qty,
            pnl_pct=position.pnl_pct(price),
            reason=reason,
        )
        self._record(trade)
        self.sync(force=True)
        return trade

    def evaluate_exits(self, prices: dict[str, float], timestamp) -> list[Trade]:
        exits: list[Trade] = []
        round_trip_cost_pct = float(os.getenv("V2_ROUND_TRIP_COST_PCT", "0.0035") or 0.0035)
        for symbol, position in list(self.positions.items()):
            price = prices.get(symbol)
            if price is None:
                logger.info(
                    "📌 position %s qty=%d entry=₩%.0f current=missing stop=₩%.0f",
                    symbol,
                    position.quantity,
                    position.entry_price,
                    position.stop_price,
                )
                continue
            position.peak_price = max(position.peak_price, price)
            pnl = position.pnl_pct(price)
            net_pnl = pnl - round_trip_cost_pct
            stop_gap_pct = (price / max(position.entry_price * (1 + self.settings.stop_loss_pct), 1) - 1.0)
            take_gross_pct = self.settings.take_profit_pct + round_trip_cost_pct
            take_price = position.entry_price * (1 + take_gross_pct)
            take_gap_pct = (take_price / max(price, 1) - 1.0)
            trail_active = net_pnl >= self.settings.trailing_start_pct
            peak_pullback_pct = (position.peak_price - price) / max(position.peak_price, 1)
            logger.info(
                "📌 position %s qty=%d entry=₩%.0f price=₩%.0f peak=₩%.0f "
                "gross=%+.2f%% net~%+.2f%% stop=₩%.0f(%+.2f%% room) "
                "take=₩%.0f(%+.2f%% left) trail=%s peak_drop=%.2f%%",
                symbol,
                position.quantity,
                position.entry_price,
                price,
                position.peak_price,
                pnl * 100,
                net_pnl * 100,
                position.entry_price * (1 + self.settings.stop_loss_pct),
                stop_gap_pct * 100,
                take_price,
                take_gap_pct * 100,
                "ON" if trail_active else "off",
                peak_pullback_pct * 100,
            )
            if pnl <= self.settings.stop_loss_pct:
                trade = self.sell(symbol, price, f"scalp stop gross={pnl * 100:.2f}% net~{net_pnl * 100:.2f}%", timestamp)
            elif net_pnl >= self.settings.take_profit_pct:
                trade = self.sell(symbol, price, f"fee-aware scalp take net~{net_pnl * 100:.2f}%", timestamp)
            elif net_pnl >= self.settings.trailing_start_pct and price <= position.peak_price * (1 - self.settings.trailing_gap_pct):
                trade = self.sell(symbol, price, f"fee-aware scalp trail net~{net_pnl * 100:.2f}%", timestamp)
            elif position.entry_time is not None and timestamp - position.entry_time >= timedelta(minutes=self.settings.time_stop_minutes) and net_pnl >= 0.002:
                trade = self.sell(symbol, price, f"fee-aware scalp time net~{net_pnl * 100:.2f}%", timestamp)
            else:
                trade = None
            if trade:
                exits.append(trade)
        return exits

    def _record(self, trade: Trade) -> None:
        self.trades.append(trade)
        payload = {
            "timestamp": trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat") else str(trade.timestamp),
            "recorded_at": datetime.now().isoformat(),
            "symbol": trade.symbol,
            "name": trade.name,
            "action": trade.action.value,
            "strategy": trade.strategy.value,
            "price": trade.price,
            "quantity": trade.quantity,
            "pnl_pct": trade.pnl_pct,
            "reason": trade.reason,
            "broker": "kis_live",
        }
        with Path(self.settings.ledger_path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
