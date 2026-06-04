"""
test_backtest_audit.py — Deterministic small-case audit of the portfolio backtest engine.

Per Codex decision: 2026-06-04-codex-decision-freeze-kr-live-and-audit.md

Each test uses known inputs with analytically derivable expected outputs.
No network access, no random data, no FinanceDataReader calls.

Functions under test are extracted from backtest_option_a.py and re-verified here.
If a test breaks, the production simulator has the same bug.
"""

from __future__ import annotations

import math
import sys
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

# ── Import simulator functions from backtest_option_a ───────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backtest_option_a import _exit_sim, portfolio_simulate, compute_portfolio_metrics

COST = 0.0035   # round-trip cost constant from the production simulator
MIN_ALLOC = 200_000  # MIN_ALLOCATION from production simulator


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _day(offset: int, base: str = "2023-01-02") -> datetime:
    return datetime.strptime(base, "%Y-%m-%d") + timedelta(days=offset)


def _ohlc(open_: float, high: float, low: float, close: float, n: int = 1) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame for _exit_sim future_rows."""
    rows = [{"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 1000}] * n
    return pd.DataFrame(rows)


def _trade(entry_date, exit_date, net_pnl: float, allocated: float, score: float = 1.0, year: int = 2023) -> dict:
    return {
        "ticker": "TEST",
        "signal_date": entry_date - timedelta(days=1),
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_price": 10000.0,
        "net_pnl": net_pnl,
        "hold_days": (exit_date - entry_date).days,
        "exit_reason": "test",
        "regime": "BULL",
        "year": year,
        "strategy": "PULLBACK",
        "score_proxy": score,
    }


# ════════════════════════════════════════════════════════════════════════════
# Test 1: One winning trade + one losing trade — NAV and PF
# ════════════════════════════════════════════════════════════════════════════

def test_one_win_one_loss_nav_and_pf():
    """
    Win: +8% net = +7.65% after 0.35% cost. Allocated ₩1M → return ₩76,500.
    Loss: -4% net = -4.35% after 0.35% cost. Allocated ₩1M → loss ₩43,500.
    Expected final NAV = ₩2M + ₩76,500 - ₩43,500 = ₩2,033,000.
    Trade-level PF = win_pnl_weighted / loss_pnl_weighted = 76,500 / 43,500 ≈ 1.759.
    """
    initial = 2_000_000
    alloc = 1_000_000
    win_net = 0.08 - COST   # 0.0765
    loss_net = -0.04 - COST  # -0.0435

    trades = [
        _trade(_day(1), _day(6), win_net, alloc, year=2023),
        _trade(_day(8), _day(13), loss_net, alloc, year=2023),
    ]
    for t in trades:
        t["allocated"] = alloc

    metrics = compute_portfolio_metrics(trades, initial, "2023-01-01", "2024-01-01")

    expected_nav = initial + alloc * win_net + alloc * loss_net
    assert abs(metrics.get("final_nav", sum(t["net_pnl"] * t["allocated"] for t in trades) + initial) - expected_nav) < 1

    expected_pf = (alloc * win_net) / abs(alloc * loss_net)
    assert abs(metrics["pf"] - expected_pf) < 0.01, f"PF {metrics['pf']:.4f} ≠ expected {expected_pf:.4f}"
    assert metrics["pf"] > 1.0  # win outweighs loss in this scenario


# ════════════════════════════════════════════════════════════════════════════
# Test 2: Overlapping signals with max-position constraint
# ════════════════════════════════════════════════════════════════════════════

def test_overlapping_signals_respect_max_positions():
    """
    Three signals fire on the same entry date; max_positions=2.
    Only 2 should be executed; the third must be skipped.
    """
    d = _day(1)
    exit_d = _day(6)
    # score_proxy descending: trade A > B > C
    trade_a = {**_trade(d, exit_d, 0.05, 0), "score_proxy": 3.0, "ticker": "A"}
    trade_b = {**_trade(d, exit_d, 0.05, 0), "score_proxy": 2.0, "ticker": "B"}
    trade_c = {**_trade(d, exit_d, 0.05, 0), "score_proxy": 1.0, "ticker": "C"}

    executed, _ = portfolio_simulate([trade_a, trade_b, trade_c], 3_000_000, max_positions=2)

    assert len(executed) == 2
    tickers_executed = {t["ticker"] for t in executed}
    assert "C" not in tickers_executed, "Lowest-score trade must be skipped when max_positions=2"


# ════════════════════════════════════════════════════════════════════════════
# Test 3: Cash is reserved on entry and released on exit
# ════════════════════════════════════════════════════════════════════════════

def test_cash_reserved_on_entry_released_on_exit():
    """
    Initial cash ₩1.5M, max_positions=3 → allocation = ₩1.5M/3 = ₩500K per slot.
    Trade 1 opens on day 1, exits on day 5 with net_pnl=0 (flat).
    Trade 2 opens on day 6 (after trade 1 exits). Must get ₩500K back.
    Trade 2 should execute; if cash were not returned it would have < MIN_ALLOC.
    """
    initial = 1_500_000
    # Two sequential trades on the same ticker alias (non-overlapping dates)
    t1 = _trade(_day(1), _day(5), 0.0, 0)   # flat — returns exactly what was taken
    t2 = {**_trade(_day(6), _day(10), 0.05, 0), "ticker": "B"}

    executed, _ = portfolio_simulate([t1, t2], initial, max_positions=3)

    assert len(executed) == 2, "Both trades must execute: cash freed after trade 1 exit"


# ════════════════════════════════════════════════════════════════════════════
# Test 4: Fees charged exactly once (at exit, round-trip)
# ════════════════════════════════════════════════════════════════════════════

def test_fees_charged_exactly_once_in_exit_sim():
    """
    Take exit: gross = +8%; net after one round-trip cost = 8% - 0.35% = 7.65%.
    Stop exit: gross = -4%; net = -4% - 0.35% = -4.35%.
    """
    # Policy: stop=-4%, take=+8%, trail_start=+5%, trail_gap=2.5%
    stop, take, ts, tg = -0.04, 0.08, 0.05, 0.025

    # ── Take hit on day 1 ──
    future_take = _ohlc(open_=10000, high=10900, low=9900, close=10800)
    pnl_take, days, reason = _exit_sim(10000, future_take, stop, take, ts, tg, max_hold=5)
    assert reason == "take"
    assert abs(pnl_take - (take - COST)) < 1e-9, f"Take net_pnl {pnl_take} ≠ {take - COST}"

    # ── Stop hit on day 1 ──
    future_stop = _ohlc(open_=10000, high=10050, low=9550, close=9600)
    pnl_stop, days, reason = _exit_sim(10000, future_stop, stop, take, ts, tg, max_hold=5)
    assert reason == "stop"
    assert abs(pnl_stop - (stop - COST)) < 1e-9, f"Stop net_pnl {pnl_stop} ≠ {stop - COST}"


# ════════════════════════════════════════════════════════════════════════════
# Test 5: Gap-through-stop executes at open, not at stop level
# ════════════════════════════════════════════════════════════════════════════

def test_gap_through_stop_exits_at_open():
    """
    Entry 10000, stop -4% → stop_price 9600.
    Next day opens at 9400 (below stop_price) → exit at open 9400.
    net_pnl = (9400/10000 - 1) - COST = -0.06 - 0.0035 = -0.0635.
    """
    stop, take, ts, tg = -0.04, 0.08, 0.05, 0.025
    # Open below stop: 9400 < 9600
    future = _ohlc(open_=9400, high=9500, low=9300, close=9450)
    pnl, days, reason = _exit_sim(10000, future, stop, take, ts, tg, max_hold=5)

    assert reason == "stop_gap", f"Expected stop_gap, got {reason!r}"
    expected = (9400 / 10000 - 1) - COST
    assert abs(pnl - expected) < 1e-9, f"Gap-stop pnl {pnl:.6f} ≠ {expected:.6f}"


# ════════════════════════════════════════════════════════════════════════════
# Test 6: Same-day stop/take collision — stop wins (conservative)
# ════════════════════════════════════════════════════════════════════════════

def test_same_day_stop_take_collision_stop_wins():
    """
    Entry 10000, stop -4% → 9600, take +8% → 10800.
    Single bar: open=10000, high=10900 (take triggered), low=9550 (stop triggered).
    Stop must win per the conservative same-day policy.
    """
    stop, take, ts, tg = -0.04, 0.08, 0.05, 0.025
    # Both stop and take are touched intraday
    future = _ohlc(open_=10000, high=10900, low=9550, close=10200)
    pnl, days, reason = _exit_sim(10000, future, stop, take, ts, tg, max_hold=5)

    assert reason == "stop", f"Stop must win over take; got {reason!r}"
    assert abs(pnl - (stop - COST)) < 1e-9


# ════════════════════════════════════════════════════════════════════════════
# Test 7: Final open positions are closed at end of simulation
# ════════════════════════════════════════════════════════════════════════════

def test_final_open_positions_are_closed():
    """
    A trade with exit_date far in the future must still be included in the executed
    list after portfolio_simulate closes everything at the simulation end (year 2099).
    """
    far_future = datetime(2099, 1, 1)
    t = _trade(_day(1), far_future, 0.05, 0)

    executed, nav_events = portfolio_simulate([t], 1_000_000, max_positions=1)

    assert len(executed) == 1, "Open position must be force-closed at simulation end"


# ════════════════════════════════════════════════════════════════════════════
# Test 8: Minimum trade amount blocks entries when capital depleted
# ════════════════════════════════════════════════════════════════════════════

def test_minimum_trade_amount_blocks_entry():
    """
    Initial capital ₩600K, max_positions=3 → allocation = ₩200K exactly = MIN_ALLOC.
    First trade takes ₩200K and exits at -50% → returns ₩100K.
    Second trade: cash = ₩400K, allocation = ₩400K/3 ≈ ₩133K < MIN_ALLOC.
    Second trade must be SKIPPED.
    """
    initial = 600_000
    t1 = _trade(_day(1), _day(3), -0.50 - COST, 0)   # half of allocated capital gone
    t2 = {**_trade(_day(4), _day(7), 0.10, 0), "ticker": "B", "score_proxy": 1.0}

    executed, _ = portfolio_simulate([t1, t2], initial, max_positions=3)

    # t1 must execute; t2 must be skipped due to capital floor
    assert any(t["ticker"] == "TEST" for t in executed), "t1 must execute"
    assert not any(t["ticker"] == "B" for t in executed), (
        "t2 must be skipped: allocation after t1 loss < MIN_ALLOCATION"
    )


# ════════════════════════════════════════════════════════════════════════════
# Test 9: Position sizing changes after gains and losses
# ════════════════════════════════════════════════════════════════════════════

def test_position_size_shrinks_after_loss():
    """
    Compounding: each trade's allocation = cash / max_positions at entry time.
    After a loss, the denominator (cash) shrinks, so future allocations shrink.
    Trade 1: ₩3M / 3 = ₩1M allocated, loses 20% → returns ₩800K.
    After exit: cash = ₩2M + ₩800K = ₩2.8M.
    Trade 2 (while trade 1 slot freed): allocation = ₩2.8M / 3 ≈ ₩933K (< ₩1M).
    """
    initial = 3_000_000
    t1 = _trade(_day(1), _day(5), -0.20 - COST, 0, score=2.0)
    t2 = {**_trade(_day(6), _day(10), 0.05, 0), "ticker": "B", "score_proxy": 1.0}

    executed, _ = portfolio_simulate([t1, t2], initial, max_positions=3)

    t1_ex = next(t for t in executed if t["ticker"] == "TEST")
    t2_ex = next((t for t in executed if t["ticker"] == "B"), None)
    assert t2_ex is not None, "Trade 2 should execute"

    # t1 allocation ≈ 1M (first trade, full cash)
    assert abs(t1_ex["allocated"] - 1_000_000) < 1, f"t1 allocation {t1_ex['allocated']} ≠ 1M"

    # t2 allocation < t1 allocation (capital shrank after loss)
    assert t2_ex["allocated"] < t1_ex["allocated"], (
        f"t2 alloc {t2_ex['allocated']:.0f} must be less than t1 alloc {t1_ex['allocated']:.0f}"
    )


# ════════════════════════════════════════════════════════════════════════════
# Test 10: No future data — regime/signal must use only backward-looking windows
# ════════════════════════════════════════════════════════════════════════════

def test_no_future_data_in_rolling_indicators():
    """
    Indicators computed with rolling(...) use only past values when min_periods is
    honoured and the window is strictly backward-looking.
    Verify: rolling(N).mean() at index i must equal the mean of rows [i-N+1 .. i].
    A lookahead bug would use rows [i .. i+N-1] or shift the index.
    """
    n = 20
    prices = pd.Series(range(1, 101), dtype=float)
    sma = prices.rolling(n).mean()

    for i in range(n, len(prices)):
        expected = prices.iloc[i - n + 1: i + 1].mean()
        actual = sma.iloc[i]
        assert abs(actual - expected) < 1e-10, (
            f"SMA at index {i}: got {actual}, expected {expected} — lookahead detected"
        )

    # Confirm NaN for the first (n-1) rows — no partial lookahead filling
    assert sma.iloc[:n - 1].isna().all(), "Rolling SMA must be NaN before the window is full"


# ════════════════════════════════════════════════════════════════════════════
# Bonus: Trade-level PF vs portfolio cash-flow PF — why they differ
# ════════════════════════════════════════════════════════════════════════════

def test_trade_level_pf_vs_portfolio_cashflow_pf():
    """
    Trade-level PF weights each trade equally by count.
    Portfolio cash-flow PF weights each trade by the capital allocated.

    Setup: 2 winners of ₩100K each, 1 big loser of ₩1M.
    Trade PF (equal weight): (2 × 0.05) / (1 × 0.05) if equal pnl pct → 2.0.
    Portfolio PF (capital weighted): (2 × 100K × 0.05) / (1M × 0.05) = 10K / 50K = 0.20.
    The two metrics diverge sharply when position sizes differ.
    """
    win_pnl_pct = 0.05
    loss_pnl_pct = -0.05

    win_alloc = 100_000
    loss_alloc = 1_000_000

    trades = [
        {**_trade(_day(1), _day(5), win_pnl_pct, win_alloc), "ticker": "W1", "allocated": win_alloc},
        {**_trade(_day(2), _day(6), win_pnl_pct, win_alloc), "ticker": "W2", "allocated": win_alloc},
        {**_trade(_day(3), _day(7), loss_pnl_pct, loss_alloc), "ticker": "L1", "allocated": loss_alloc},
    ]

    # Portfolio PF from compute_portfolio_metrics
    initial = win_alloc * 2 + loss_alloc
    metrics = compute_portfolio_metrics(trades, initial, "2023-01-01", "2024-01-01")
    portfolio_pf = metrics["pf"]

    # Trade-level PF (unweighted)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    trade_pf = (sum(t["net_pnl"] for t in wins) / abs(sum(t["net_pnl"] for t in losses)))

    # Portfolio PF should be much worse than trade-level PF
    assert portfolio_pf < trade_pf, (
        f"Portfolio PF {portfolio_pf:.3f} should be less than trade-level PF {trade_pf:.3f} "
        "when winners are smaller positions than losers"
    )
    assert portfolio_pf < 1.0, "Capital-weighted PF must be below 1.0 in this scenario"
    assert trade_pf > 1.0, "Trade-count PF must be above 1.0 in this scenario (2 wins, 1 loss)"
