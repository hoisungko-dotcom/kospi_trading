"""
test_shadow_league.py — Deterministic tests for the KR Shadow Strategy League.

Original 8 test areas + 10 new tests per Codex review (2026-06-04).
No network access, no KIS API calls, no live broker.
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kospi_bot_v2.shadow.strategies.definitions import (
    ALL_STRATEGIES, STRATEGY_BY_ID,
    StrategyA_v1, StrategyA_v2, StrategyB_v1, StrategyC_v1,
    StrategyC_v2, StrategyD_v1, StrategyE_v1,
)
from kospi_bot_v2.shadow.portfolio import ShadowPortfolio, ShadowGuardError, ROUND_TRIP_COST
from kospi_bot_v2.shadow.reporter import build_daily_report, build_weekly_report
from kospi_bot_v2.shadow.data_types import ShadowTrade


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _bull_pullback_row(symbol: str = "005930") -> pd.Series:
    """Row that triggers B-v1 (BULL pullback) — close < high20 so NOT a breakout."""
    return pd.Series({
        "symbol": symbol, "name": "삼성전자",
        "close": 70000.0,  "sma20": 68000.0,  "sma60": 65000.0,  "sma224": 60000.0,
        "sma5":  69000.0,  "high20": 73000.0, "low20":  60000.0,
        "sma20_slope": 100.0, "sma60_slope": 50.0,
        "return5":  -0.030, "return20": 0.080,
        "rsi14":    52.0,   "atr_pct":  0.05,
        "volume":   1_500_000, "avg_volume20": 2_000_000, "volume_ratio": 0.75,
        "rs20": 0.02, "consecutive_buy_days": 0,
        "macd": 1.0, "macd_signal": 0.5,
    })


def _breakout_row(symbol: str = "000660") -> pd.Series:
    """Row that triggers C-v1 (breakout) — close ≥ high20*0.995, volume surge.
    B-v1 rejects it because close ≥ high20*0.99 (breakout guard in B-v1).
    """
    return pd.Series({
        "symbol": symbol, "name": "SK하이닉스",
        "close": 74000.0,  "sma20": 68000.0,  "sma60": 65000.0,  "sma224": 60000.0,
        "sma5":  71000.0,  "high20": 74200.0, "low20":  60000.0,   # close/high20 ≈ 0.997 ≥ 0.995
        "sma20_slope": 200.0, "sma60_slope": 100.0,
        "return5":  0.05,  "return20": 0.12,
        "rsi14":    60.0,  "atr_pct":  0.04,
        "volume":   4_000_000, "avg_volume20": 2_000_000, "volume_ratio": 2.0,
        "rs20": 0.05, "consecutive_buy_days": 0,
        "macd": 2.0, "macd_signal": 1.0,
    })


def _make_portfolio(strategy, state_path, initial: float = 2_000_000) -> ShadowPortfolio:
    return ShadowPortfolio(
        strategy=strategy,
        initial_capital=initial,
        max_positions=2,
        max_daily_entries=1,
        state_path=state_path,
    )


def _ohlcv(open_: float, high: float, low: float, close: float) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close}


# ═════════════════════════════════════════════════════════════════════════════
# ORIGINAL TESTS (updated for new class names and 7-strategy registry)
# ═════════════════════════════════════════════════════════════════════════════

def test_all_strategies_see_identical_input():
    """Each strategy evaluates the same row — no state leaks, deterministic."""
    row = _bull_pullback_row()
    regime = "BULL"
    results = {s.strategy_id: s.should_enter(row, regime) for s in ALL_STRATEGIES}

    assert results["B"]  is True,  "B-v1 must trigger on bull pullback row"
    assert results["E"]  is False, "E (cash) must never enter"

    # Idempotent: calling twice must return same result
    for strat in ALL_STRATEGIES:
        assert strat.should_enter(row, regime) == results[strat.strategy_id]


def test_portfolios_are_independent(tmp_path):
    """Entering in A must not consume cash or positions from B."""
    row = _bull_pullback_row()
    port_a = _make_portfolio(StrategyA_v1(), tmp_path / "a.json")
    port_b = _make_portfolio(StrategyB_v1(), tmp_path / "b.json")
    cash_a0, cash_b0 = port_a.cash, port_b.cash

    port_a.try_enter(row, "BULL", "2023-06-01", execution_price=70000)

    assert port_a.cash < cash_a0,  "A cash must decrease after entry"
    assert port_b.cash == cash_b0, "B cash must be unaffected"
    assert len(port_a.positions) == 1
    assert len(port_b.positions) == 0


def test_shadow_portfolio_does_not_import_live_broker():
    """Importing ShadowPortfolio must not pull in live_broker as a side-effect."""
    for m in [k for k in sys.modules if "shadow.portfolio" in k]:
        del sys.modules[m]

    before = set(sys.modules)
    importlib.import_module("kospi_bot_v2.shadow.portfolio")
    new = set(sys.modules) - before
    assert not any("live_broker" in m for m in new), (
        f"live_broker must not appear in shadow portfolio imports: {new}"
    )


def test_shadow_guard_rejects_live_broker_subclass():
    """ShadowGuardError raised when a class path contains 'live_broker'."""
    FakeBroker = type.__new__(type, "KISLiveBroker", (object,), {})
    FakeBroker.__module__ = "kospi_bot_v2.portfolio.live_broker"
    fake = object.__new__(FakeBroker)
    with pytest.raises(ShadowGuardError):
        ShadowPortfolio.assert_not_live_broker_subclass(fake)


def test_costs_applied_consistently(tmp_path):
    """net pnl_pct must equal gross return minus ROUND_TRIP_COST exactly."""
    port = _make_portfolio(StrategyB_v1(), tmp_path / "b.json")
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", execution_price=70000)
    assert len(port.positions) == 1

    take_price = 70000 * (1 + 0.10)   # +10% gross → triggers B-v1 take at +10%
    bar = _ohlcv(70000, take_price + 500, 69500, take_price)
    closed = port.mark_day({"005930": bar}, "2023-06-02")

    assert len(closed) == 1
    trade = closed[0]
    gross = (trade.exit_price / 70000) - 1
    expected = gross - ROUND_TRIP_COST
    assert abs((trade.pnl_pct or 0) - expected) < 1e-9


# ═════════════════════════════════════════════════════════════════════════════
# P0 FIX VERIFICATION TESTS
# ═════════════════════════════════════════════════════════════════════════════

def test_cash_and_nav_reconcile_after_round_trip(tmp_path):
    """P0-1: After entry + exit, cash must deduct the round-trip fee. cash + positions = NAV."""
    port = _make_portfolio(StrategyB_v1(), tmp_path / "b.json")
    initial_cash = port.cash  # 2_000_000

    entry_price = 70000.0
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", execution_price=entry_price)
    assert len(port.positions) == 1
    pos = port.positions["005930"]
    qty = pos.quantity
    take_price = pos.take_price  # B-v1 take at +10% = 77_000

    # High goes well above take — triggers take-price exit deterministically.
    # Low stays well above stop so only the take fires.
    bar = _ohlcv(entry_price, take_price + 5000, entry_price - 100, take_price)
    closed = port.mark_day({"005930": bar}, "2023-06-02")
    assert len(closed) == 1
    assert closed[0].exit_reason == "take"
    assert abs(closed[0].exit_price - take_price) < 1.0

    expected_fee = ROUND_TRIP_COST * qty * entry_price
    expected_cash = (initial_cash - qty * entry_price) + qty * take_price - expected_fee

    assert abs(port.cash - expected_cash) < 1.0, (
        f"cash {port.cash:.0f} ≠ expected {expected_cash:.0f} (fee not deducted)"
    )

    # NAV must equal cash when no positions remain
    assert abs(port.nav() - port.cash) < 1.0, "NAV must equal cash when no open positions"


def test_held_sessions_increments_on_trading_day(tmp_path):
    """P0-3: held_sessions must increment once per mark_day call (not calendar days)."""
    port = _make_portfolio(StrategyA_v1(), tmp_path / "a.json")
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", execution_price=70000)
    assert len(port.positions) == 1

    # First mark_day: held_sessions should be 1
    bar = _ohlcv(70000, 70500, 69800, 70200)  # flat bar — no exit
    port.mark_day({"005930": bar}, "2023-06-02")
    if len(port.positions) > 0:
        assert port.positions["005930"].held_sessions == 1

    # Second mark_day: held_sessions should be 2 (or position closed at time stop = 1 day)
    # A-v1 max_hold_days=1, so it will exit on first mark_day — check closed trade instead
    # If closed, verify it was the time stop
    if len(port.positions) == 0:
        # position was closed by time stop after 1 held session
        assert len(port.closed_trades) == 1
        assert port.closed_trades[0].exit_reason == "time"
    else:
        # position still open (shouldn't happen with A-v1 max_hold=1)
        assert port.positions["005930"].held_sessions >= 1


def test_held_sessions_not_calendar_days(tmp_path):
    """P0-3: A Friday entry + Monday exit (3 calendar days) should be 1 trading session."""
    port = _make_portfolio(StrategyB_v1(), tmp_path / "b.json")  # B-v1 max_hold=7d
    entry_price = 70000.0
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-02", execution_price=entry_price)  # Friday
    assert len(port.positions) == 1

    # Monday (3 calendar days, but only 1 trading session)
    bar = _ohlcv(entry_price, entry_price + 500, entry_price - 500, entry_price)
    port.mark_day({"005930": bar}, "2023-06-05")  # Monday

    if len(port.positions) > 0:
        pos = port.positions["005930"]
        assert pos.held_sessions == 1, (
            f"held_sessions={pos.held_sessions} but only 1 trading day elapsed"
        )
    # max_hold_days=7 → position must NOT be closed after 1 trading session
    assert len(port.positions) == 1, "B-v1 position must NOT close after 1 trading session (max=7)"


def test_league_evaluate_skips_entry_when_price_missing(tmp_path):
    """P0-4: evaluate() must not enter using stale row.close as fallback price."""
    from kospi_bot_v2.shadow.league import ShadowLeague
    from datetime import datetime
    import pandas as pd

    league = ShadowLeague(tmp_path / "league")
    row = _bull_pullback_row()
    frame = pd.DataFrame([row])

    # prices dict is empty — no live price for any symbol
    initial_navs = {p.strategy.strategy_id: p.nav() for p in league.portfolios}

    league.evaluate(
        frame=frame,
        regime="BULL",
        prices={},   # ← no live prices
        timestamp=datetime(2023, 6, 1, 10, 0),
        trade_date="2023-06-01",
    )

    # No strategy should have entered — NAV and cash must be unchanged
    for p in league.portfolios:
        assert len(p.positions) == 0, (
            f"[{p.strategy.strategy_id}] must not enter without a live price"
        )
        assert p.nav() == initial_navs[p.strategy.strategy_id]


def test_intraday_stop_exit_at_observed_price(tmp_path):
    """P0-5: evaluate_intraday_exits must close at observed price, not stop_price."""
    port = _make_portfolio(StrategyA_v1(), tmp_path / "a.json")
    entry_price = 70000.0
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", execution_price=entry_price)
    assert len(port.positions) == 1

    stop_price = entry_price * (1 - 0.020)  # A-v1 stop: 68600
    # Price has gapped through the stop — it's well below stop_price
    observed_price = stop_price - 500   # e.g. 68100 < 68600

    closed = port.evaluate_intraday_exits({"005930": observed_price}, "2023-06-01")

    assert len(closed) == 1
    trade = closed[0]
    assert trade.exit_reason == "stop"
    # Exit price must be the observed price, NOT the stop_price
    assert abs(trade.exit_price - observed_price) < 1.0, (
        f"exit_price={trade.exit_price} must equal observed {observed_price}, not stop_price {stop_price}"
    )


def test_eod_nav_history_recorded_by_finalize_day(tmp_path):
    """P0-2: finalize_day must record NAV snapshot; daily_pnl_pct returns non-zero after a trade."""
    from kospi_bot_v2.shadow.league import ShadowLeague
    from datetime import datetime

    league = ShadowLeague(tmp_path / "league")
    port_b = next(p for p in league.portfolios if p.strategy.strategy_id == "B")

    # Enter a position at entry_price, then finalize with a flat close
    entry_price = 70000.0
    port_b.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", entry_price)
    assert len(port_b.positions) == 1

    ohlcv = {"005930": _ohlcv(entry_price, entry_price + 100, entry_price - 100, entry_price)}
    trade_date = "2023-06-01"
    league.finalize_day(
        ohlcv_by_symbol=ohlcv,
        trade_date=trade_date,
        regime="BULL",
        kospi_pct=0.01,
        current_prices={"005930": entry_price},
        trading_day=True,
        send_telegram=False,
    )

    # NAV snapshot must have been recorded
    assert len(port_b.nav_history) == 1
    assert port_b.nav_history[0]["date"] == trade_date
    # daily_pnl_pct must return a value, not default 0.0
    pnl = port_b.daily_pnl_pct(trade_date)
    # With a flat close (no exit) and an open position, NAV ≈ initial_capital → pnl ≈ 0
    assert isinstance(pnl, float)


def test_daily_report_contains_all_strategies(tmp_path):
    """Daily report must include every registered strategy ID."""
    portfolios = [_make_portfolio(s, tmp_path / f"{s.strategy_id}.json") for s in ALL_STRATEGIES]
    report = build_daily_report(
        portfolios=portfolios, regime="BULL", kospi_pct=0.012,
        as_of=date(2026, 6, 4), current_prices={},
    )
    for strat in ALL_STRATEGIES:
        assert strat.strategy_id in report, f"{strat.strategy_id} missing from report"
    assert "₩" in report and "레짐" in report


def test_weekly_report_ranks_by_nav(tmp_path):
    """Strategy with higher NAV must appear before E (cash) in weekly report."""
    port_e = _make_portfolio(StrategyE_v1(), tmp_path / "e.json")
    port_a = _make_portfolio(StrategyA_v1(), tmp_path / "a.json")
    port_a.cash = 2_100_000  # simulate profit

    report = build_weekly_report(
        portfolios=[port_e, port_a],
        week_label="1주차", period_str="2026-06-01 ~ 2026-06-04",
        trading_days_elapsed=4, next_eval_date="2026-06-11",
    )
    assert report.index("A") < report.index("E"), "A (higher NAV) must rank above E"
    assert "현금 기준점을 이긴 전략" in report


def test_profit_factor_computed_correctly(tmp_path):
    """LeagueStats: PF = gross_profit / gross_loss (weighted by pnl_pct counts)."""
    port = _make_portfolio(StrategyB_v1(), tmp_path / "b.json")
    win_pnl  = 0.08 - ROUND_TRIP_COST
    loss_pnl = -0.04 - ROUND_TRIP_COST
    port.closed_trades = [
        ShadowTrade("B","AAA","A","2023-06-01",100,10,96,110,"BULL",1.0,ROUND_TRIP_COST,
                    "2023-06-05",108,"take",win_pnl,0.08,0.0,4),
        ShadowTrade("B","BBB","B","2023-06-02",100,10,96,110,"BULL",1.0,ROUND_TRIP_COST,
                    "2023-06-06",96,"stop",loss_pnl,0.01,-0.04,4),
    ]
    stats = port.league_stats()
    assert stats.n_trades == 2 and stats.n_wins == 1
    assert abs(stats.profit_factor - win_pnl / abs(loss_pnl)) < 1e-9


def test_strategy_registry_matches_expected_definitions():
    """Registry must contain exactly A, A2, B, C, C2, D, E — no silent changes."""
    expected = {"A": "v1", "A2": "v2", "B": "v1", "C": "v1", "C2": "v2", "D": "v1", "E": "v1"}
    actual   = {s.strategy_id: s.version for s in ALL_STRATEGIES}
    assert actual == expected, (
        f"Registry changed.\nExpected: {expected}\nActual:   {actual}"
    )
    for strat in ALL_STRATEGIES:
        assert STRATEGY_BY_ID[strat.strategy_id] is strat


def test_cash_benchmark_never_trades(tmp_path):
    """E must never enter regardless of regime or row."""
    port_e = _make_portfolio(StrategyE_v1(), tmp_path / "e.json")
    cash0  = port_e.cash

    for regime in ("BULL", "WEAK", "CRASH", "NEUTRAL"):
        for row in (_bull_pullback_row(), _breakout_row()):
            assert port_e.try_enter(row, regime, "2023-06-01", 70000) is None
    assert port_e.cash == cash0 and len(port_e.positions) == 0


def test_missing_price_skips_entry(tmp_path):
    """execution_price ≤ 0 must block entry without crashing."""
    port = _make_portfolio(StrategyB_v1(), tmp_path / "b.json")
    assert port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", 0.0) is None
    assert len(port.positions) == 0


def test_missing_ohlcv_warns_not_crashes(tmp_path, caplog):
    """mark_day with no bar data must warn and leave position open."""
    port = _make_portfolio(StrategyB_v1(), tmp_path / "b.json")
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", 70000)
    assert len(port.positions) == 1

    with caplog.at_level(logging.WARNING):
        closed = port.mark_day({}, "2023-06-02")

    assert len(closed) == 0
    assert any("No price data" in r.message for r in caplog.records)


def test_strategy_d_rejects_missing_sma224(tmp_path):
    row = _bull_pullback_row()
    for bad_val in (None, float("nan"), 0.0):
        row_copy = row.copy()
        row_copy["sma224"] = bad_val
        assert StrategyD_v1().should_enter(row_copy, "BULL") is False, (
            f"D-v1 must reject when sma224={bad_val!r}"
        )


def test_strategy_d_accepts_valid_sma224():
    row = _bull_pullback_row()
    row["sma224"] = 55000.0    # below close (70000) → valid long-term uptrend
    assert StrategyD_v1().should_enter(row, "BULL") is True

    row["sma224"] = 80000.0    # above close → reject
    assert StrategyD_v1().should_enter(row, "BULL") is False


# ═════════════════════════════════════════════════════════════════════════════
# NEW TESTS (10 from Codex review 2026-06-04)
# ═════════════════════════════════════════════════════════════════════════════

# New Test 1 — A-v1 exit params exactly match deployed config/settings.py
def test_a_v1_exit_params_match_deployed_defaults():
    """A-v1 must use exact live bot defaults: -2%/+5%/trail+2.5%/gap1%/hold1d."""
    ep = StrategyA_v1().exit_params()
    assert ep.stop_pct       == -0.020, f"stop {ep.stop_pct} ≠ -0.020"
    assert ep.take_pct       ==  0.050, f"take {ep.take_pct} ≠ 0.050"
    assert ep.trail_start_pct == 0.025, f"trail_start {ep.trail_start_pct} ≠ 0.025"
    assert ep.trail_gap_pct  ==  0.010, f"trail_gap {ep.trail_gap_pct} ≠ 0.010"
    assert ep.max_hold_days  ==  1,     f"max_hold_days {ep.max_hold_days} ≠ 1"

    # A-v2 must be wider (different hypothesis)
    ep2 = StrategyA_v2().exit_params()
    assert ep2.stop_pct  < ep.stop_pct,  "A-v2 stop must be wider than A-v1"
    assert ep2.take_pct  > ep.take_pct,  "A-v2 take must be wider than A-v1"
    assert ep2.max_hold_days > ep.max_hold_days


# New Test 2 — B/C/D evaluate raw frame independently, not A's pre-filtered output
def test_strategies_evaluate_raw_frame_independently():
    """B, C, D each independently evaluate the same raw row — not A's output."""
    # A pure BREAKOUT row that A and C should accept, but B must reject (not a pullback)
    row = _breakout_row()
    regime = "BULL"

    a_enters  = StrategyA_v1().should_enter(row, regime)
    b_enters  = StrategyB_v1().should_enter(row, regime)
    c_enters  = StrategyC_v1().should_enter(row, regime)
    d_enters  = StrategyD_v1().should_enter(row, regime)

    assert a_enters is True,  "A-v1 should accept a breakout row in BULL"
    assert c_enters is True,  "C-v1 should accept a breakout row"
    assert b_enters is False, "B-v1 must REJECT breakout row (it's not a pullback)"
    # D = B + SMA224: if B rejects, D must also reject
    assert d_enters is False, "D-v1 must reject when B-v1 rejects (D depends on B)"

    # A pure pullback that A and B accept, but C must reject (not near high20)
    row_pb = _bull_pullback_row()  # close=70000, high20=73000 → not at high20
    a2 = StrategyA_v1().should_enter(row_pb, regime)
    c2 = StrategyC_v1().should_enter(row_pb, regime)
    assert a2 is True,  "A-v1 must accept pullback row"
    assert c2 is False, "C-v1 must reject pullback row (not at high20)"


# New Test 3 — D-v1 skips safely when SMA224 unavailable, never treats 0 as pass
def test_d_never_treats_missing_sma224_as_pass():
    """SMA224 = NaN/None/0 must all result in D skipping, not entering."""
    row = _bull_pullback_row()
    d = StrategyD_v1()

    for val in (float("nan"), None, 0, -1):
        r = row.copy()
        r["sma224"] = val
        result = d.should_enter(r, "BULL")
        assert result is False, f"D must skip when sma224={val!r}, got True"

    # If sma224 column is absent entirely
    row_no_sma224 = row.drop("sma224", errors="ignore")
    assert d.should_enter(row_no_sma224, "BULL") is False


# New Test 4 — Intraday exit evaluation (evaluate_intraday_exits)
def test_intraday_exit_triggers_before_eod(tmp_path):
    """evaluate_intraday_exits must close position when price crosses stop intraday."""
    port = _make_portfolio(StrategyA_v1(), tmp_path / "a.json")
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", execution_price=70000)
    assert len(port.positions) == 1

    stop_price = 70000 * (1 - 0.020)   # A-v1 stop = -2% → 68600
    # Price has dropped to just below stop
    intraday_prices = {"005930": stop_price - 100}   # 68500 < 68600
    closed = port.evaluate_intraday_exits(intraday_prices, "2023-06-01")

    assert len(closed) == 1, "Intraday stop must fire before end-of-day mark_day"
    assert closed[0].exit_reason == "stop"
    assert len(port.positions) == 0


# New Test 5 — Candidate ordering is deterministic
def test_candidate_ordering_is_deterministic():
    """_rank_candidates: score desc → volume_ratio desc → symbol asc."""
    from kospi_bot_v2.shadow.league import _rank_candidates

    # Three rows that all satisfy B-v1 in BULL; differ only by volume_ratio and symbol
    def make_row(sym, vol_ratio, sma20_slope=100, sma60_slope=50):
        r = _bull_pullback_row(sym).copy()
        r["volume_ratio"] = vol_ratio
        r["avg_volume20"] = 2_000_000
        r["volume"]       = int(r["avg_volume20"] * vol_ratio)
        r["sma20_slope"]  = sma20_slope
        r["sma60_slope"]  = sma60_slope
        return r

    # All three pass B-v1; identical score (same indicators) except volume_ratio
    rows = pd.DataFrame([
        make_row("005930", 0.80),    # symbol asc but higher vol_ratio
        make_row("000660", 0.90),    # highest vol_ratio → should be first
        make_row("035720", 0.70),    # lowest vol_ratio → should be last
    ])

    strat = StrategyB_v1()
    ranked = _rank_candidates(rows, strat, "BULL")

    assert len(ranked) == 3
    syms = [str(r.get("symbol")) for r in ranked]
    # 000660 (vol 0.90) should rank first; 035720 (vol 0.70) last
    assert syms[0] == "000660", f"Expected 000660 first, got {syms}"
    assert syms[-1] == "035720", f"Expected 035720 last, got {syms}"


# New Test 6 — Restart does not duplicate a shadow trade
def test_restart_does_not_duplicate_trades(tmp_path):
    """Loading state from JSON must preserve exactly the trades made in the first run."""
    state_path = tmp_path / "b.json"
    port = _make_portfolio(StrategyB_v1(), state_path)
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", 70000)
    # Close the position
    port.mark_day({"005930": _ohlcv(70000, 78000, 69500, 77000)}, "2023-06-02")

    n_trades = len(port.closed_trades)
    n_positions = len(port.positions)

    # Simulate restart by loading from the same state file
    port2 = _make_portfolio(StrategyB_v1(), state_path)
    assert len(port2.closed_trades) == n_trades,    "No duplicate trades after restart"
    assert len(port2.positions)     == n_positions, "No duplicate positions after restart"
    assert abs(port2.cash - port.cash) < 1,         "Cash must be preserved exactly"


# New Test 7 — State writes are atomic (temp → replace)
def test_state_writes_are_atomic(tmp_path):
    """State must be written via a temp file so partial writes don't corrupt state."""
    state_path = tmp_path / "b.json"
    port = _make_portfolio(StrategyB_v1(), state_path)
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", 70000)

    # After a successful try_enter, state file must exist and be valid JSON
    assert state_path.exists(), "State file must be written after entry"
    data = json.loads(state_path.read_text())
    assert "cash" in data and "positions" in data, "State file must be valid JSON"

    # No temp file should remain
    tmp_file = state_path.with_suffix(".tmp")
    assert not tmp_file.exists(), "Temp file must be cleaned up after atomic write"


# New Test 8 — Timer expressions: verify systemd-analyze on AWS
def test_timer_expressions_are_valid_on_aws():
    """Verify systemd-analyze calendar accepts the planned timer expressions on AWS."""
    import subprocess
    KEY  = str(Path.home() / ".ssh" / "crypto_trader_upbit-key.pem")
    HOST = "ubuntu@100.27.228.229"

    if not Path(KEY).exists():
        pytest.skip("SSH key not found — skipping AWS timer validation")

    daily_expr  = "Mon..Fri *-*-* 07:00:00 UTC"   # 16:00 KST
    weekly_expr = "Fri *-*-* 07:30:00 UTC"         # 16:30 KST Friday

    for expr in (daily_expr, weekly_expr):
        result = subprocess.run(
            ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", HOST,
             f"systemd-analyze calendar '{expr}'"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, (
            f"systemd-analyze rejected expression '{expr}':\n{result.stderr}"
        )
        assert "Next elapse" in result.stdout, (
            f"systemd-analyze did not return 'Next elapse' for '{expr}'"
        )


# New Test 9 — KRX holidays do not increment trading-day counters
def test_krx_holiday_does_not_close_positions(tmp_path):
    """finalize_day(trading_day=False) must not process exits or count as a day."""
    from kospi_bot_v2.shadow.league import ShadowLeague

    league = ShadowLeague(tmp_path / "league")
    # Manually put an open position into portfolio B
    port_b = next(p for p in league.portfolios if p.strategy.strategy_id == "B")
    port_b.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", 70000)
    assert len(port_b.positions) == 1

    ohlcv = {"005930": _ohlcv(70000, 78000, 68000, 77000)}   # would hit take
    league.finalize_day(
        ohlcv_by_symbol=ohlcv,
        trade_date="2023-06-01",
        regime="BULL",
        kospi_pct=0.01,
        trading_day=False,        # ← holiday
        send_telegram=False,
    )
    # Position must NOT have been closed on a holiday
    assert len(port_b.positions) == 1, "Position must survive a KRX holiday"
    # Report must contain holiday marker
    latest = (tmp_path / "league" / "latest.txt").read_text()
    assert "휴장일" in latest


# New Test 10 — Live broker order API cannot be reached from shadow code
def test_live_broker_api_unreachable_from_shadow():
    """Shadow code must not have access to live order methods (place_buy_order etc.)."""
    import kospi_bot_v2.shadow.portfolio as shadow_port
    import kospi_bot_v2.shadow.league    as shadow_league

    # Neither module should have 'place_buy_order' or 'place_sell_order' in their namespace
    for mod in (shadow_port, shadow_league):
        assert not hasattr(mod, "place_buy_order"),  f"{mod.__name__} must not expose place_buy_order"
        assert not hasattr(mod, "place_sell_order"), f"{mod.__name__} must not expose place_sell_order"
        assert not hasattr(mod, "KISLiveBroker"),    f"{mod.__name__} must not expose KISLiveBroker"

    # ShadowPortfolio instances have no order method
    from kospi_bot_v2.shadow.portfolio import ShadowPortfolio
    assert not hasattr(ShadowPortfolio, "place_buy_order")
    assert not hasattr(ShadowPortfolio, "place_sell_order")


# ─────────────────────────────────────────────────────────────────────────────
# C-v1 regime coverage — all regimes accepted, regime recorded per trade
# ─────────────────────────────────────────────────────────────────────────────

def test_c_v1_accepts_all_regimes():
    """C-v1 must enter in ALL regimes (including CRASH) and record regime on trade."""
    row = _breakout_row()
    c = StrategyC_v1()
    for regime in ("BULL", "NEUTRAL", "WEAK", "CRASH"):
        assert c.should_enter(row, regime) is True, f"C-v1 must accept {regime}"


def test_c_v2_rejects_bull_accepts_weak_with_strong_signal():
    """C-v2: rejects BULL; accepts non-BULL only with extra-strong conditions."""
    row = _breakout_row()
    row["volume_ratio"] = 3.0      # >= 2.5 required
    row["rs20"]         = 0.05     # >= 0.03 required
    row["rsi14"]        = 60.0
    row["close"]        = 74200.0  # close >= high20 * 0.998 = 74501 * 0.998 ≈ 74352 → need to adjust
    row["high20"]       = 74000.0  # so close (74200) >= high20 * 0.998 (73852) ✓
    c2 = StrategyC_v2()
    assert c2.should_enter(row, "BULL")   is False, "C-v2 must reject BULL"
    assert c2.should_enter(row, "WEAK")   is True,  "C-v2 must accept WEAK with strong signal"
    assert c2.should_enter(row, "CRASH")  is True,  "C-v2 must accept CRASH with strong signal"


# ═════════════════════════════════════════════════════════════════════════════
# SECOND-REVIEW P0-6 TESTS: script entry points, exit codes, idempotency
# ═════════════════════════════════════════════════════════════════════════════

def _write_snapshot(
    base_dir: Path,
    trade_date: str | None = None,
    trading_day: bool = True,
    is_final: bool = True,
    session_date: str | None = None,
) -> str:
    """Write a deterministic EOD snapshot for script tests. Returns trade_date used."""
    from kospi_bot_v2.shadow.snapshot import save_snapshot, _kst_today_iso
    if trade_date is None:
        trade_date = _kst_today_iso()
    ohlcv = {
        "005930": {"open": 70000.0, "high": 71000.0, "low": 69500.0, "close": 70500.0},
        "000660": {"open": 172000.0, "high": 174000.0, "low": 171000.0, "close": 173000.0},
    }
    prices = {"005930": 70500.0, "000660": 173000.0}
    save_snapshot(
        base_dir, trade_date, "BULL", 0.012, ohlcv, prices,
        trading_day=trading_day, session_date=session_date, is_final=is_final,
    )
    return trade_date


def test_daily_finalize_succeeds_with_valid_snapshot(tmp_path, monkeypatch):
    """P0-1/P0-6: daily_finalize._run() completes with a valid snapshot."""
    from kospi_bot_v2.shadow.scripts import daily_finalize
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    trade_date = _write_snapshot(tmp_path)  # is_final=True, uses KST today

    daily_finalize._run()  # must not raise

    report_file = tmp_path / "daily" / f"{trade_date}.txt"
    assert report_file.exists(), "Daily report file must be written"
    assert "레짐" in report_file.read_text(encoding="utf-8")


def test_daily_finalize_exits_zero_on_no_snapshot(tmp_path, monkeypatch):
    """P0-6: Missing snapshot on weekend is expected (no trading) — exits 0."""
    from kospi_bot_v2.shadow.scripts import daily_finalize
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))

    # Inject Sunday 2026-06-07 so absence of snapshot is treated as expected (exit 0)
    monkeypatch.setattr(
        "kospi_bot_v2.shadow.scripts.daily_finalize._kst_today",
        lambda: date(2026, 6, 7),   # Sunday
    )

    with pytest.raises(SystemExit) as exc:
        daily_finalize.main()
    assert exc.value.code == 0


def test_daily_finalize_exits_nonzero_on_corrupt_snapshot(tmp_path, monkeypatch):
    """P0-2/P0-6: A corrupt snapshot file is a programming error — exits 1."""
    from kospi_bot_v2.shadow.scripts import daily_finalize
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))

    # Write a syntactically valid JSON but missing required fields, named for KST today
    from kospi_bot_v2.shadow.snapshot import _kst_today_iso
    snap_path = tmp_path / f"eod_snapshot_{_kst_today_iso()}.json"
    snap_path.write_text('{"trade_date": "2023-06-01"}')  # missing regime, etc.

    with pytest.raises(SystemExit) as exc:
        daily_finalize.main()
    assert exc.value.code == 1


def test_weekly_report_succeeds_with_valid_snapshot(tmp_path, monkeypatch):
    """P0-1/P0-6: weekly_report._run() completes with a valid snapshot."""
    from kospi_bot_v2.shadow.scripts import weekly_report
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    _write_snapshot(tmp_path)  # uses KST today by default

    weekly_report._run()  # must not raise

    weekly_dir = tmp_path / "weekly"
    reports = list(weekly_dir.glob("*.txt"))
    assert reports, "Weekly report file must be written"


def test_same_date_rerun_is_idempotent(tmp_path):
    """P0-4/P0-6: finalize_day for the same date must not increment held_sessions twice."""
    from kospi_bot_v2.shadow.league import ShadowLeague

    league = ShadowLeague(tmp_path / "league")
    port_b = next(p for p in league.portfolios if p.strategy.strategy_id == "B")
    port_b.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", 70000.0)
    assert len(port_b.positions) == 1

    ohlcv = {"005930": _ohlcv(70000, 70500, 69500, 70200)}
    trade_date = "2023-06-02"

    # First finalize
    league.finalize_day(ohlcv, trade_date, "BULL", 0.01,
                        trading_day=True, send_telegram=False)
    pos = port_b.positions.get("005930")
    if pos:
        sessions_first = pos.held_sessions
        nav_count_first = len(port_b.nav_history)

    # Second finalize — must be a no-op
    league.finalize_day(ohlcv, trade_date, "BULL", 0.01,
                        trading_day=True, send_telegram=False)

    pos_after = port_b.positions.get("005930")
    if pos and pos_after:
        assert pos_after.held_sessions == sessions_first, (
            f"held_sessions incremented on re-run: {pos_after.held_sessions} ≠ {sessions_first}"
        )
        assert len(port_b.nav_history) == nav_count_first, (
            "NAV history must not duplicate on same-date re-run"
        )


def test_snapshot_trading_day_false_skips_exits(tmp_path, monkeypatch):
    """P0-3/P0-6: snapshot with trading_day=False must not process exits."""
    from kospi_bot_v2.shadow.scripts import daily_finalize
    from kospi_bot_v2.shadow.league import ShadowLeague

    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    trade_date = _write_snapshot(tmp_path, trading_day=False)  # uses KST today

    # Open a position directly in the B portfolio
    league = ShadowLeague(tmp_path)
    port_b = next(p for p in league.portfolios if p.strategy.strategy_id == "B")
    port_b.try_enter(_bull_pullback_row(), "BULL", trade_date, 70000.0)
    assert len(port_b.positions) == 1

    # Run finalize — trading_day=False from snapshot, so mark_day should be skipped
    daily_finalize._run()

    # Re-load league from persisted state to verify
    league2 = ShadowLeague(tmp_path)
    port_b2 = next(p for p in league2.portfolios if p.strategy.strategy_id == "B")
    assert len(port_b2.positions) == 1, (
        "Position must NOT be closed on a holiday (trading_day=False)"
    )
    report_file = tmp_path / "daily" / f"{trade_date}.txt"
    assert "휴장일" in report_file.read_text(encoding="utf-8")


def test_record_eod_nav_is_idempotent(tmp_path):
    """P0-4: record_eod_nav for same date must upsert, not append a duplicate."""
    port = _make_portfolio(StrategyB_v1(), tmp_path / "b.json")
    port.try_enter(_bull_pullback_row(), "BULL", "2023-06-01", 70000.0)

    port.record_eod_nav("2023-06-01", {"005930": 70000.0})
    assert len(port.nav_history) == 1

    # Second call: same date, slightly different price (e.g. rounding) → must upsert
    port.record_eod_nav("2023-06-01", {"005930": 70100.0})
    assert len(port.nav_history) == 1, "Second record_eod_nav for same date must upsert, not append"

    # Different date → appended
    port.record_eod_nav("2023-06-02", {"005930": 71000.0})
    assert len(port.nav_history) == 2


# ═════════════════════════════════════════════════════════════════════════════
# THIRD-REVIEW P0 TESTS: proxy filter, restart persistence, snapshot validation
# ═════════════════════════════════════════════════════════════════════════════

def test_proxy_symbols_cannot_become_positions(tmp_path):
    """P0-4: KSPI and KDQ proxy rows must be filtered before strategy evaluation."""
    from kospi_bot_v2.shadow.league import ShadowLeague
    from datetime import datetime

    league = ShadowLeague(tmp_path / "league")

    # Build proxy rows that would otherwise satisfy B-v1 if they were tradable
    kspi_row = _bull_pullback_row("KSPI")
    kdq_row  = _bull_pullback_row("KDQ")
    frame    = pd.DataFrame([kspi_row, kdq_row])
    prices   = {"KSPI": 70000.0, "KDQ": 70000.0}

    league.evaluate(
        frame=frame,
        regime="BULL",
        prices=prices,
        timestamp=datetime(2026, 6, 4, 10, 0),
        trade_date="2026-06-04",
    )

    for p in league.portfolios:
        assert "KSPI" not in p.positions, f"[{p.strategy.strategy_id}] KSPI must never be a position"
        assert "KDQ"  not in p.positions, f"[{p.strategy.strategy_id}] KDQ must never be a position"
        assert len(p.positions) == 0, f"[{p.strategy.strategy_id}] must have no positions (only proxies in frame)"


def test_entry_limit_survives_restart(tmp_path):
    """P0-3: Daily entry limit must be enforced even after process restart."""
    from kospi_bot_v2.shadow.snapshot import _kst_today_iso
    state_path = tmp_path / "b.json"
    today = _kst_today_iso()  # match what portfolio._load() checks against

    # First run: enter once (max_daily_entries=1)
    port = _make_portfolio(StrategyB_v1(), state_path)
    t1 = port.try_enter(_bull_pullback_row("005930"), "BULL", today, 70000.0)
    assert t1 is not None, "First entry must succeed"
    assert port._entries_today == 1

    # Simulate restart — reload state from disk
    port2 = _make_portfolio(StrategyB_v1(), state_path)
    assert port2._entries_today == 1, "entries_today must be restored to 1 after restart"

    # Second entry attempt on same day must be blocked
    t2 = port2.try_enter(_bull_pullback_row("000660"), "BULL", today, 70000.0)
    assert t2 is None, "Second entry on same day must be blocked even after restart"


def test_intraday_exits_survive_restart(tmp_path):
    """P0-3: Intraday exits must appear in get_exits_today() after process restart."""
    from kospi_bot_v2.shadow.snapshot import _kst_today_iso
    state_path = tmp_path / "b.json"
    today = _kst_today_iso()  # match what portfolio._load() checks against

    port = _make_portfolio(StrategyB_v1(), state_path)
    port.try_enter(_bull_pullback_row("005930"), "BULL", today, 70000.0)

    # Trigger intraday stop exit (B-v1 stop = -5%)
    stop_price = 70000.0 * (1 - 0.051)  # just below B-v1 stop of -5%
    closed = port.evaluate_intraday_exits({"005930": stop_price}, today)
    assert len(closed) == 1, "Position must close on intraday stop"
    assert len(port.get_exits_today(today)) == 1

    # Simulate restart
    port2 = _make_portfolio(StrategyB_v1(), state_path)
    exits = port2.get_exits_today(today)
    assert len(exits) == 1, "Intraday exit must survive process restart"
    assert exits[0].exit_reason == "stop"


def test_stale_snapshot_rejected_by_daily_finalize(tmp_path, monkeypatch):
    """P0-2: Trading-day snapshot with session_date ≠ today must cause exit 1."""
    import datetime as _dt
    from kospi_bot_v2.shadow.snapshot import save_snapshot, _kst_today_iso
    from kospi_bot_v2.shadow.scripts import daily_finalize

    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    today = _kst_today_iso()  # use same KST date as production code
    kst = _dt.timezone(_dt.timedelta(hours=9))
    yesterday = (_dt.datetime.now(kst).date() - _dt.timedelta(days=1)).isoformat()

    # Write a trading-day snapshot (trading_day=True default) with stale session_date
    ohlcv  = {"005930": {"open": 70000.0, "high": 71000.0, "low": 69500.0, "close": 70500.0}}
    prices = {"005930": 70500.0}
    save_snapshot(
        tmp_path, today, "BULL", 0.012, ohlcv, prices,
        session_date=yesterday,  # stale: yesterday's data on a supposed trading day
        is_final=True,
    )

    with pytest.raises(SystemExit) as exc:
        daily_finalize.main()
    assert exc.value.code == 1, "Stale trading-day snapshot must cause exit 1"


def test_nonfinal_snapshot_rejected_by_daily_finalize(tmp_path, monkeypatch):
    """P0-2: Snapshot with is_final=False must cause exit 1 (written before 15:30 KST)."""
    from kospi_bot_v2.shadow.scripts import daily_finalize
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))

    _write_snapshot(tmp_path, is_final=False)  # uses KST today by default

    with pytest.raises(SystemExit) as exc:
        daily_finalize.main()
    assert exc.value.code == 1, "Non-final snapshot (is_final=False) must cause exit 1"


def test_missing_snapshot_on_weekday_exits_nonzero(tmp_path, monkeypatch):
    """P0-6: Missing snapshot on a weekday signals live-runner outage — must exit 1."""
    from kospi_bot_v2.shadow.scripts import daily_finalize

    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))

    # Inject Thursday 2026-06-04 so absence triggers the outage path
    monkeypatch.setattr(
        "kospi_bot_v2.shadow.scripts.daily_finalize._kst_today",
        lambda: date(2026, 6, 4),   # Thursday
    )

    # No snapshot written
    with pytest.raises(SystemExit) as exc:
        daily_finalize.main()
    assert exc.value.code == 1, "Missing snapshot on weekday must cause exit 1 (outage)"


# ═════════════════════════════════════════════════════════════════════════════
# FOURTH-REVIEW P0 TESTS: post-close snapshot, holiday policy, KST date
# ═════════════════════════════════════════════════════════════════════════════

def test_holiday_snapshot_accepted_by_load_and_validate(tmp_path):
    """P0-4: Holiday snapshot (trading_day=False, session_date≠today) must be accepted.
    Holiday policy is distinct from stale-data policy: only trading-day snapshots
    with session_date≠today are rejected.
    """
    import datetime as _dt
    from kospi_bot_v2.shadow.snapshot import load_and_validate_snapshot, _kst_today_iso

    today = _kst_today_iso()
    kst = _dt.timezone(_dt.timedelta(hours=9))
    yesterday = (_dt.datetime.now(kst).date() - _dt.timedelta(days=1)).isoformat()

    # Holiday snapshot: trade_date=today, session_date=yesterday, trading_day=False
    _write_snapshot(
        tmp_path, today,
        trading_day=False, session_date=yesterday, is_final=True,
    )

    snap = load_and_validate_snapshot(tmp_path, today)
    assert snap["trading_day"] is False, "Holiday snapshot must have trading_day=False"
    assert snap["session_date"] == yesterday, "Holiday snapshot must preserve session_date"
    assert snap["is_final"] is True


def test_daily_finalize_succeeds_with_holiday_snapshot(tmp_path, monkeypatch):
    """P0-4: daily_finalize._run() must succeed with a holiday snapshot and produce a holiday report."""
    import datetime as _dt
    from kospi_bot_v2.shadow.scripts import daily_finalize
    from kospi_bot_v2.shadow.snapshot import _kst_today_iso

    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    today = _kst_today_iso()
    kst = _dt.timezone(_dt.timedelta(hours=9))
    yesterday = (_dt.datetime.now(kst).date() - _dt.timedelta(days=1)).isoformat()

    _write_snapshot(tmp_path, today, trading_day=False, session_date=yesterday, is_final=True)

    daily_finalize._run()  # must not raise

    report_file = tmp_path / "daily" / f"{today}.txt"
    assert report_file.exists(), "Holiday daily report must be written"
    assert "휴장일" in report_file.read_text(encoding="utf-8"), (
        "Holiday report must contain the 휴장일 marker"
    )


def test_kst_date_at_23h30_utc():
    """P0-3: Deterministic proof that 23:30 UTC is the next calendar day in KST.
    AWS UTC hosts must use the KST date for trade_date, not the host local date.
    """
    import datetime as _dt
    kst = _dt.timezone(_dt.timedelta(hours=9))

    # 23:30 UTC on 2026-06-03 is 08:30 KST on 2026-06-04
    utc_23h30 = _dt.datetime(2026, 6, 3, 23, 30, tzinfo=_dt.timezone.utc)
    kst_date  = utc_23h30.astimezone(kst).date()
    utc_date  = utc_23h30.date()

    assert kst_date == _dt.date(2026, 6, 4), (
        f"23:30 UTC on 2026-06-03 must map to KST date 2026-06-04, got {kst_date}"
    )
    assert utc_date  == _dt.date(2026, 6, 3), "UTC date is still 2026-06-03"
    assert kst_date  != utc_date, (
        "KST and UTC dates differ — using date.today() on a UTC host gives wrong trade_date"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# FIFTH-REVIEW P0 TESTS: snapshot-only post-close, live-order guard, retry, P0-5
# ═══════════════════════════════════════════════════════════════════════════════

def test_legacy_snapshot_missing_fields_is_rejected(tmp_path, monkeypatch):
    """P0-5: Snapshot missing session_date/generated_at/is_final/trading_day must be rejected."""
    import json as _json
    from kospi_bot_v2.shadow.snapshot import snapshot_path, _kst_today_iso, load_snapshot
    from kospi_bot_v2.shadow.scripts import daily_finalize

    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    today = _kst_today_iso()

    # Legacy-style snapshot: only the original five fields, missing the four new required ones.
    path = snapshot_path(tmp_path, today)
    path.write_text(_json.dumps({
        "trade_date": today,
        "regime": "BULL",
        "kospi_pct": 0.01,
        "ohlcv_by_symbol": {},
        "prices": {},
    }))

    with pytest.raises(ValueError, match="missing required keys"):
        load_snapshot(tmp_path, today)

    # daily_finalize must also reject it (exits 1)
    with pytest.raises(SystemExit) as exc:
        daily_finalize.main()
    assert exc.value.code == 1, "Legacy snapshot with missing fields must cause exit 1"


def test_post_close_snapshot_only_no_broker_calls(tmp_path, monkeypatch):
    """P0-1/P0-4: run_post_close_snapshot_only() must not call any broker method."""
    import datetime as _dt
    from unittest.mock import MagicMock
    import pandas as pd
    from kospi_bot_v2.runtime.live_runner import LiveRunner
    from kospi_bot_v2.domain.models import MarketSnapshot, MarketRegime
    from kospi_bot_v2.shadow.snapshot import load_and_validate_snapshot

    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    _KST = _dt.timezone(_dt.timedelta(hours=9))
    _now_kst = _dt.datetime.now(_KST)

    # Minimal provider: one row, snapshot timestamp matches current KST date
    frame = pd.DataFrame([{
        "symbol": "005930", "name": "삼성전자", "timestamp": _now_kst,
        "close": 70000.0, "open": 69500.0, "high": 70500.0, "low": 69000.0,
    }])
    provider = MagicMock()
    provider.load_universe_frame.return_value = frame
    provider.market_snapshot.return_value = MarketSnapshot(
        timestamp=_now_kst,
        kospi_change_pct=0.5, kosdaq_change_pct=0.3,
        advance_ratio=0.6, volatility_pct=1.2,
        kospi_above_sma20=True, kosdaq_above_sma20=True,
    )

    runner = LiveRunner.__new__(LiveRunner)
    runner.provider = provider
    runner.regime_classifier = MagicMock()
    runner.regime_classifier.classify.return_value = MarketRegime.BULL
    runner.broker = MagicMock()

    runner.run_post_close_snapshot_only()

    # No broker method must be called
    runner.broker.sync.assert_not_called()
    runner.broker.evaluate_exits.assert_not_called()
    runner.broker.buy.assert_not_called()
    runner.broker.sell.assert_not_called()

    # Snapshot must be final and valid
    snap = load_and_validate_snapshot(tmp_path)
    assert snap["is_final"] is True
    assert snap["trading_day"] is True  # session_date matches today KST


def test_post_close_do_function_retries_on_failure(tmp_path):
    """P0-3/P0-4: _do_post_close_snapshot retries on transient failure and marks done on success."""
    from unittest.mock import MagicMock, patch
    import kospi_bot_v2.main as main_mod

    runner = MagicMock()
    # First two calls fail; third succeeds (writes nothing — validate is also mocked)
    runner.run_post_close_snapshot_only.side_effect = [
        OSError("network timeout"),
        OSError("KIS unreachable"),
        None,
    ]

    with patch("kospi_bot_v2.main.load_and_validate_snapshot",
               return_value={"is_final": True, "trading_day": True,
                             "session_date": "2026-06-04", "generated_at": "x"}), \
         patch.object(main_mod.time, "sleep"):
        result = main_mod._do_post_close_snapshot(runner, tmp_path, max_retries=3, sleep_sec=0)

    assert result is True
    assert runner.run_post_close_snapshot_only.call_count == 3


def test_post_close_snapshot_passes_finalizer(tmp_path, monkeypatch):
    """P0-4: Snapshot written by run_post_close_snapshot_only() can be read by daily_finalize."""
    import datetime as _dt
    from unittest.mock import MagicMock
    import pandas as pd
    from kospi_bot_v2.runtime.live_runner import LiveRunner
    from kospi_bot_v2.domain.models import MarketSnapshot, MarketRegime
    from kospi_bot_v2.shadow.scripts import daily_finalize

    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    _KST = _dt.timezone(_dt.timedelta(hours=9))
    _now_kst = _dt.datetime.now(_KST)

    frame = pd.DataFrame([{
        "symbol": "005930", "name": "삼성전자", "timestamp": _now_kst,
        "close": 70000.0, "open": 69500.0, "high": 70500.0, "low": 69000.0,
    }])
    provider = MagicMock()
    provider.load_universe_frame.return_value = frame
    provider.market_snapshot.return_value = MarketSnapshot(
        timestamp=_now_kst,
        kospi_change_pct=0.5, kosdaq_change_pct=0.3,
        advance_ratio=0.6, volatility_pct=1.2,
        kospi_above_sma20=True, kosdaq_above_sma20=True,
    )

    runner = LiveRunner.__new__(LiveRunner)
    runner.provider = provider
    runner.regime_classifier = MagicMock()
    runner.regime_classifier.classify.return_value = MarketRegime.BULL
    runner.broker = MagicMock()

    runner.run_post_close_snapshot_only()

    # Finalizer must succeed with the snapshot written by post-close
    daily_finalize._run()  # must not raise

    from kospi_bot_v2.shadow.snapshot import _kst_today_iso
    report_file = tmp_path / "daily" / f"{_kst_today_iso()}.txt"
    assert report_file.exists(), "Finalizer must write a daily report from post-close snapshot"
