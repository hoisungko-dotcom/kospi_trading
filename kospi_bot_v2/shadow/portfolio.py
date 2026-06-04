"""
ShadowPortfolio — independent paper-trading account for one strategy.

Guard invariants (enforced at runtime):
- Never imports live_broker.
- Never calls KIS order APIs.
- Never touches live positions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

_KST = timezone(timedelta(hours=9))
from typing import Optional

import pandas as pd

from kospi_bot_v2.shadow.data_types import (
    LeagueStats, ShadowPosition, ShadowTrade
)
from kospi_bot_v2.shadow.strategies.definitions import BaseShadowStrategy

logger = logging.getLogger(__name__)

ROUND_TRIP_COST = 0.0035

# Dataclass field names — used to filter __dict__ keys that are valid constructor args
_TRADE_FIELDS = frozenset(ShadowTrade.__dataclass_fields__.keys())
_POSITION_FIELDS = frozenset(ShadowPosition.__dataclass_fields__.keys())


class ShadowGuardError(RuntimeError):
    """Raised if shadow code attempts to touch the live broker."""


class ShadowPortfolio:
    """Independent paper-trading portfolio for one strategy.

    State is persisted to a single JSON file per strategy so that positions
    survive service restarts. The file is updated after every trade event.

    Daily counters (_entries_today, _entries_today_log, _exits_today_log) are
    persisted and restored if the loaded state's today_str matches today.
    This ensures daily entry limits and exit reports survive process restarts.
    """

    def __init__(
        self,
        strategy: BaseShadowStrategy,
        initial_capital: float,
        max_positions: int,
        max_daily_entries: int,
        state_path: Path,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.max_daily_entries = max_daily_entries
        self.state_path = state_path

        self.cash: float = initial_capital
        self.positions: dict[str, ShadowPosition] = {}
        self.closed_trades: list[ShadowTrade] = []
        self.nav_history: list[dict] = []   # [{"date": "YYYY-MM-DD", "nav": float}, ...]

        # Daily in-memory counters — persisted and restored across restarts (P0-3)
        self._today_str: str = ""
        self._entries_today: int = 0
        self._entries_today_log: list[ShadowTrade] = []
        self._exits_today_log: list[ShadowTrade] = []

        if state_path.exists():
            self._load()

    # ── Guard ──────────────────────────────────────────────────────────────

    @staticmethod
    def assert_not_live_broker_subclass(instance: object) -> None:
        cls_name = type(instance).__module__ + "." + type(instance).__qualname__
        if "live_broker" in cls_name:
            raise ShadowGuardError(
                f"ShadowPortfolio must not wrap a live broker instance. Got: {cls_name}"
            )

    # ── Daily counter management ──────────────────────────────────────────

    def _ensure_today(self, trade_date: str) -> None:
        """Reset in-memory daily counters when a new trading day begins."""
        if trade_date != self._today_str:
            self._entries_today = 0
            self._entries_today_log = []
            self._exits_today_log = []
            self._today_str = trade_date

    # ── Entry ──────────────────────────────────────────────────────────────

    def try_enter(
        self,
        row: pd.Series,
        regime: str,
        trade_date: str,
        execution_price: float,
    ) -> Optional[ShadowTrade]:
        """Evaluate strategy signal and shadow-enter if conditions are met.

        Returns the ShadowTrade record if entered, None otherwise.
        Does NOT call any live broker method.
        """
        self._ensure_today(trade_date)

        if not self.strategy.should_enter(row, regime):
            return None
        symbol = str(row.get("symbol", ""))
        if symbol in self.positions:
            return None
        if len(self.positions) >= self.max_positions:
            return None
        if self._entries_today >= self.max_daily_entries:
            return None
        if execution_price <= 0:
            return None

        alloc = self.cash / self.max_positions
        alloc = min(alloc, self.cash)
        if alloc < 100_000:  # minimum meaningful allocation
            return None

        ep = self.strategy.exit_params()
        quantity = max(1, int(alloc / execution_price))
        actual_alloc = quantity * execution_price

        stop_p = execution_price * (1 + ep.stop_pct)
        take_p = execution_price * (1 + ep.take_pct)
        trail_start = execution_price * (1 + ep.trail_start_pct)

        pos = ShadowPosition(
            symbol=symbol,
            name=str(row.get("name", symbol)),
            strategy_id=self.strategy.strategy_id,
            quantity=quantity,
            entry_price=execution_price,
            entry_date=trade_date,
            stop_price=stop_p,
            take_price=take_p,
            trail_start_price=trail_start,
            trail_gap=ep.trail_gap_pct,
            regime=regime,
            score=float(self.strategy.score(row)),
            peak_price=execution_price,
        )
        trade = ShadowTrade(
            strategy_id=self.strategy.strategy_id,
            symbol=symbol,
            name=pos.name,
            entry_date=trade_date,
            entry_price=execution_price,
            quantity=quantity,
            stop_price=stop_p,
            take_price=take_p,
            regime=regime,
            score=pos.score,
            cost_pct=ROUND_TRIP_COST,
        )

        self.cash -= actual_alloc
        self.positions[symbol] = pos
        self._entries_today += 1
        self._entries_today_log.append(trade)

        logger.info(
            "📋 [%s %s] SHADOW ENTER %s @ ₩%.0f qty=%d stop=%.0f take=%.0f",
            self.strategy.strategy_id, self.strategy.version,
            symbol, execution_price, quantity, stop_p, take_p,
        )
        self._save()
        return trade

    # ── Exit ───────────────────────────────────────────────────────────────

    def mark_day(
        self,
        ohlcv_by_symbol: dict[str, dict],
        trade_date: str,
    ) -> list[ShadowTrade]:
        """Check exits for all open positions using today's OHLCV bar.

        Returns list of trades that were closed today.
        Increments held_sessions for every open position (trading-day counter).
        """
        self._ensure_today(trade_date)
        ep = self.strategy.exit_params()
        closed: list[ShadowTrade] = []
        to_remove: list[str] = []

        for symbol, pos in self.positions.items():
            bar = ohlcv_by_symbol.get(symbol)
            if not bar:
                logger.warning("[%s] No price data for %s on %s",
                               self.strategy.strategy_id, symbol, trade_date)
                continue

            pos.held_sessions += 1  # P0-3: trading-day counter

            op  = float(bar.get("open",  pos.entry_price))
            hi  = float(bar.get("high",  pos.entry_price))
            lo  = float(bar.get("low",   pos.entry_price))
            cl  = float(bar.get("close", pos.entry_price))

            pos.update_extremes(hi)
            exit_price, reason = None, None

            if op <= pos.stop_price:
                exit_price, reason = op, "stop_gap"
            elif lo <= pos.stop_price:
                exit_price, reason = pos.stop_price, "stop"
            elif hi >= pos.take_price:
                exit_price, reason = pos.take_price, "take"
            else:
                if hi > pos.peak_price:
                    pos.peak_price = hi
                if pos.peak_price >= pos.trail_start_price:
                    trail_level = pos.peak_price * (1 - pos.trail_gap)
                    if lo <= trail_level:
                        exit_price, reason = trail_level, "trail"
                if exit_price is None and pos.held_sessions >= ep.max_hold_days:
                    exit_price, reason = cl, "time"

            if exit_price is not None:
                trade = self._close_position(pos, exit_price, reason, trade_date)
                closed.append(trade)
                to_remove.append(symbol)

        for s in to_remove:
            del self.positions[s]

        self._exits_today_log.extend(closed)  # P1-2: include mark_day exits

        if closed or to_remove:
            self._save()
        return closed

    def _close_position(
        self, pos: ShadowPosition, exit_price: float, reason: str, exit_date: str
    ) -> ShadowTrade:
        # P0-1: deduct round-trip fee from cash
        exit_proceeds = pos.quantity * exit_price
        entry_cost    = pos.quantity * pos.entry_price
        self.cash += exit_proceeds - ROUND_TRIP_COST * entry_cost

        trade = ShadowTrade(
            strategy_id=pos.strategy_id,
            symbol=pos.symbol,
            name=pos.name,
            entry_date=pos.entry_date,
            entry_price=pos.entry_price,
            quantity=pos.quantity,
            stop_price=pos.stop_price,
            take_price=pos.take_price,
            regime=pos.regime,
            score=pos.score,
            cost_pct=ROUND_TRIP_COST,
            mfe_pct=pos.mfe_pct,
            mae_pct=pos.mae_pct,
        )
        trade.close(exit_date, exit_price, reason, ROUND_TRIP_COST)
        self.closed_trades.append(trade)

        sign = "✅" if (trade.pnl_pct or 0) > 0 else "❌"
        logger.info(
            "%s [%s] SHADOW EXIT %s @ ₩%.0f reason=%s pnl=%.2f%%",
            sign, pos.strategy_id, pos.symbol,
            exit_price, reason, (trade.pnl_pct or 0) * 100,
        )
        return trade

    def evaluate_intraday_exits(
        self,
        prices: dict[str, float],
        trade_date: str,
    ) -> list[ShadowTrade]:
        """Check open positions against current spot prices (stop/take only)."""
        self._ensure_today(trade_date)
        closed: list[ShadowTrade] = []
        to_remove: list[str] = []

        for symbol, pos in self.positions.items():
            price = prices.get(symbol)
            if price is None:
                continue
            price = float(price)
            pos.update_extremes(price)

            net_pnl = (price / pos.entry_price) - 1
            reason = None
            exit_price = None

            if net_pnl <= self.strategy.exit_params().stop_pct:
                reason, exit_price = "stop", price  # P0-5: observed price
            elif net_pnl >= self.strategy.exit_params().take_pct:
                reason, exit_price = "take", pos.take_price

            if reason is not None and exit_price is not None:
                trade = self._close_position(pos, exit_price, reason, trade_date)
                closed.append(trade)
                to_remove.append(symbol)

        for s in to_remove:
            del self.positions[s]

        self._exits_today_log.extend(closed)  # P1-2 + P0-3

        if to_remove:
            self._save()
        return closed

    def get_entries_today(self, trade_date: str) -> list[ShadowTrade]:
        if trade_date != self._today_str:
            return []
        return list(self._entries_today_log)

    def get_exits_today(self, trade_date: str) -> list[ShadowTrade]:
        """ALL exits today: intraday + mark_day. Persisted across restarts (P0-3, P1-2)."""
        if trade_date != self._today_str:
            return []
        return list(self._exits_today_log)

    # ── NAV History ────────────────────────────────────────────────────────

    def record_eod_nav(
        self, trade_date: str, current_prices: dict[str, float] | None = None
    ) -> float:
        """Snapshot end-of-day NAV. Upserts by date (idempotent, P0-4)."""
        n = self.nav(current_prices)
        for i, snap in enumerate(self.nav_history):
            if snap["date"] == trade_date:
                self.nav_history[i]["nav"] = n
                self._save()
                return n
        self.nav_history.append({"date": trade_date, "nav": n})
        self._save()
        return n

    def daily_pnl_pct(self, trade_date: str) -> float:
        for i, snap in enumerate(self.nav_history):
            if snap["date"] == trade_date:
                prev = self.nav_history[i - 1]["nav"] if i > 0 else self.initial_capital
                return (snap["nav"] / prev) - 1
        return 0.0

    # ── Metrics ────────────────────────────────────────────────────────────

    def nav(self, current_prices: dict[str, float] | None = None) -> float:
        current_prices = current_prices or {}
        value = self.cash
        for sym, pos in self.positions.items():
            price = current_prices.get(sym, pos.entry_price)
            value += pos.quantity * price
        return value

    def league_stats(self) -> LeagueStats:
        stats = LeagueStats(strategy_id=self.strategy.strategy_id)
        for t in self.closed_trades:
            if t.pnl_pct is None:
                continue
            stats.n_trades += 1
            stats.sum_pnl_pct += t.pnl_pct
            monetary = t.realized_pnl_amount  # P1-1: monetary PF
            if monetary > 0:
                stats.n_wins += 1
                stats.gross_profit += monetary
            else:
                stats.gross_loss += abs(monetary)

        # P0-2: MDD from equity curve
        peak = self.initial_capital
        for snap in self.nav_history:
            n = snap["nav"]
            peak = max(peak, n)
            stats.max_drawdown = min(stats.max_drawdown, (n / peak) - 1)

        stats.current_nav_pct = (self.nav() / self.initial_capital) - 1
        peak_nav = max((s["nav"] for s in self.nav_history), default=self.initial_capital)
        stats.peak_nav_pct = (peak_nav / self.initial_capital) - 1
        return stats

    # ── Persistence ────────────────────────────────────────────────────────

    def _save(self) -> None:
        state = {
            "strategy_id":        self.strategy.strategy_id,
            "version":            self.strategy.version,
            "initial_capital":    self.initial_capital,
            "cash":               self.cash,
            "positions":          {sym: pos.__dict__ for sym, pos in self.positions.items()},
            "closed_trades":      [t.__dict__ for t in self.closed_trades],
            "nav_history":        self.nav_history,
            # P0-3: persist daily logs so restarts don't lose them
            "today_str":          self._today_str,
            "entries_today_count": self._entries_today,
            "entries_today_log":  [t.__dict__ for t in self._entries_today_log],
            "exits_today_log":    [t.__dict__ for t in self._exits_today_log],
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        tmp.replace(self.state_path)

    def _load(self) -> None:
        try:
            raw = self.state_path.read_text()
            state = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._quarantine(e)
            return

        try:
            self.cash = float(state.get("cash", self.initial_capital))
            self.nav_history = state.get("nav_history", [])

            for sym, d in state.get("positions", {}).items():
                d.setdefault("held_sessions", 0)  # backward compat
                self.positions[sym] = ShadowPosition(
                    **{k: v for k, v in d.items() if k in _POSITION_FIELDS}
                )
            for d in state.get("closed_trades", []):
                self.closed_trades.append(
                    ShadowTrade(**{k: v for k, v in d.items() if k in _TRADE_FIELDS})
                )

            # P0-3: restore daily logs if they're from today (KST date — correct on UTC hosts)
            loaded_today = state.get("today_str", "")
            if loaded_today == datetime.now(_KST).date().isoformat():
                self._today_str = loaded_today
                self._entries_today = state.get("entries_today_count", 0)
                for d in state.get("entries_today_log", []):
                    self._entries_today_log.append(
                        ShadowTrade(**{k: v for k, v in d.items() if k in _TRADE_FIELDS})
                    )
                for d in state.get("exits_today_log", []):
                    self._exits_today_log.append(
                        ShadowTrade(**{k: v for k, v in d.items() if k in _TRADE_FIELDS})
                    )

            logger.info(
                "[%s] Loaded: cash=₩%.0f positions=%d trades=%d "
                "nav_snaps=%d today=%s entries=%d exits=%d",
                self.strategy.strategy_id, self.cash,
                len(self.positions), len(self.closed_trades),
                len(self.nav_history), self._today_str,
                self._entries_today, len(self._exits_today_log),
            )
        except (KeyError, TypeError, ValueError) as e:
            # P1-1: corrupt state → quarantine and start fresh
            self._quarantine(e)

    def _quarantine(self, error: Exception) -> None:
        """Rename corrupt state file and start fresh. Always logs as ERROR. (P1-1)"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        corrupt_path = self.state_path.with_suffix(f".corrupt.{stamp}")
        try:
            self.state_path.rename(corrupt_path)
            logger.error(
                "[%s] CORRUPT STATE quarantined to %s — starting fresh. Error: %s",
                self.strategy.strategy_id, corrupt_path, error,
            )
        except Exception as rename_err:
            logger.error(
                "[%s] CORRUPT STATE — could not quarantine (%s), starting fresh. Error: %s",
                self.strategy.strategy_id, rename_err, error,
            )
