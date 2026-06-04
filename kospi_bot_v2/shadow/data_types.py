"""Data types for the shadow strategy league. All state is serialisable to JSON."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class ShadowPosition:
    symbol: str
    name: str
    strategy_id: str
    quantity: int
    entry_price: float
    entry_date: str         # ISO date string "YYYY-MM-DD"
    stop_price: float
    take_price: float
    trail_start_price: float
    trail_gap: float        # fraction below peak for trailing stop
    regime: str
    score: float
    peak_price: float
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    held_sessions: int = 0  # trading days held (NOT calendar days)

    def update_extremes(self, current_price: float) -> None:
        if current_price > self.peak_price:
            self.peak_price = current_price
        gain = (current_price / self.entry_price) - 1
        loss = (current_price / self.entry_price) - 1
        self.mfe_pct = max(self.mfe_pct, gain)
        self.mae_pct = min(self.mae_pct, loss)


@dataclass
class ShadowTrade:
    strategy_id: str
    symbol: str
    name: str
    entry_date: str         # ISO date
    entry_price: float
    quantity: int
    stop_price: float
    take_price: float
    regime: str
    score: float
    cost_pct: float
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None   # stop/stop_gap/take/trail/time
    pnl_pct: Optional[float] = None     # net after cost_pct
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    hold_days: Optional[int] = None

    def close(self, exit_date: str, exit_price: float, reason: str, cost_pct: float) -> None:
        self.exit_date = exit_date
        self.exit_price = exit_price
        self.exit_reason = reason
        self.pnl_pct = (exit_price / self.entry_price - 1) - cost_pct
        if self.entry_date and exit_date:
            d0 = date.fromisoformat(self.entry_date)
            d1 = date.fromisoformat(exit_date)
            self.hold_days = (d1 - d0).days

    @property
    def realized_pnl_amount(self) -> float:
        """Net monetary P&L in ₩. Correct for variable allocation sizes."""
        if self.pnl_pct is None:
            return 0.0
        return self.pnl_pct * self.quantity * self.entry_price


@dataclass
class DailySnapshot:
    """NAV and metrics as of end of one trading day."""
    strategy_id: str
    as_of: str              # ISO date
    nav: float
    cash: float
    open_positions: int
    daily_pnl_pct: float
    cumulative_pnl_pct: float
    entries_today: int
    exits_today: int


@dataclass
class LeagueStats:
    """Rolling statistics for a strategy across all closed trades."""
    strategy_id: str
    n_trades: int = 0
    n_wins: int = 0
    gross_profit: float = 0.0   # sum of positive realized ₩ PnL (monetary)
    gross_loss: float = 0.0     # sum of abs(negative) realized ₩ PnL (monetary)
    sum_pnl_pct: float = 0.0   # sum of pnl_pct for avg display only
    max_drawdown: float = 0.0
    peak_nav_pct: float = 0.0   # highest cumulative pnl since start
    current_nav_pct: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_trades if self.n_trades else 0.0

    @property
    def profit_factor(self) -> float:
        """Monetary PF — correct for variable allocation sizes (P1-1)."""
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else float("inf")

    @property
    def avg_pnl_pct(self) -> float:
        """Average per-trade return percentage (for display)."""
        return self.sum_pnl_pct / self.n_trades if self.n_trades else 0.0
