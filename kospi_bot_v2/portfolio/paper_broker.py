from __future__ import annotations

import json
from pathlib import Path

from kospi_bot_v2.config.settings import ShadowSettings
from kospi_bot_v2.domain.models import Position, Signal, SignalAction, Trade


class PaperBroker:
    """Virtual broker for shadow trading.

    This class intentionally has no live order method. It only records virtual
    fills so v2 can run next to the current production bot without touching the
    account.
    """

    def __init__(self, settings: ShadowSettings):
        self.settings = settings
        self.cash = settings.initial_cash
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.settings.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def equity(self, prices: dict[str, float] | None = None) -> float:
        prices = prices or {}
        value = self.cash
        for symbol, position in self.positions.items():
            value += position.market_value(prices.get(symbol, position.entry_price))
        return value

    def buy(self, signal: Signal, quantity: int) -> Trade | None:
        cost = signal.price * quantity
        if quantity <= 0 or cost > self.cash or signal.symbol in self.positions:
            return None
        stop_price = signal.price * (1 + self.settings.stop_loss_pct)
        position = Position(
            symbol=signal.symbol,
            name=signal.name,
            strategy=signal.strategy,
            entry_time=signal.metadata.get("timestamp") or signal.metadata.get("evaluated_at"),
            entry_price=signal.price,
            quantity=quantity,
            peak_price=signal.price,
            stop_price=stop_price,
            metadata=signal.metadata,
        )
        self.cash -= cost
        self.positions[signal.symbol] = position
        trade = Trade(
            timestamp=position.entry_time,
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
        return trade

    def sell(self, symbol: str, price: float, reason: str, timestamp) -> Trade | None:
        position = self.positions.pop(symbol, None)
        if position is None:
            return None
        self.cash += price * position.quantity
        trade = Trade(
            timestamp=timestamp,
            symbol=position.symbol,
            name=position.name,
            action=SignalAction.SELL,
            strategy=position.strategy,
            price=price,
            quantity=position.quantity,
            pnl_pct=position.pnl_pct(price),
            reason=reason,
        )
        self._record(trade)
        return trade

    def evaluate_exits(self, prices: dict[str, float], timestamp) -> list[Trade]:
        exits: list[Trade] = []
        for symbol, position in list(self.positions.items()):
            price = prices.get(symbol)
            if price is None:
                continue
            position.peak_price = max(position.peak_price, price)
            pnl = position.pnl_pct(price)
            if pnl <= self.settings.stop_loss_pct:
                trade = self.sell(symbol, price, "stop loss", timestamp)
            elif pnl >= self.settings.take_profit_pct:
                trade = self.sell(symbol, price, "take profit", timestamp)
            elif pnl >= self.settings.trailing_start_pct and price <= position.peak_price * (1 - self.settings.trailing_gap_pct):
                trade = self.sell(symbol, price, "trailing stop", timestamp)
            else:
                trade = None
            if trade:
                exits.append(trade)
        return exits

    def _record(self, trade: Trade) -> None:
        self.trades.append(trade)
        payload = {
            "timestamp": trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat") else str(trade.timestamp),
            "symbol": trade.symbol,
            "name": trade.name,
            "action": trade.action.value,
            "strategy": trade.strategy.value,
            "price": trade.price,
            "quantity": trade.quantity,
            "pnl_pct": trade.pnl_pct,
            "reason": trade.reason,
        }
        with Path(self.settings.ledger_path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
