"""
backtest_option_a.py — Option A: Extended Period + Portfolio-Level Simulation
=============================================================================
Period  : 2018-01-01 → 2026-06-04  (8 years, warmup from 2016)
Universe: Current KOSPI top 150 + KOSDAQ top 75 (market-cap ranked today)

SURVIVORSHIP BIAS WARNING:
  Universe is selected by 2026 market cap. Stocks that underperformed and
  were delisted or demoted between 2018-2026 are absent. Results represent
  an upper bound — actual achievable returns in live trading would be lower.

AUDIT COMPLIANCE (per Codex decision doc):
  - Regime: KOSPI 20d return as-of signal-day close (no lookahead)
  - Indicators: rolling backward-only windows (no lookahead)
  - Entry: next-day open (signal not acted on until following day open)
  - Same-day stop+take conflict: stop wins (conservative)
  - Gap-through stop: if next-day open <= stop_price → exit at open
  - Costs: 0.35% round-trip, applied at exit
  - Portfolio: ₩5M initial, max 3 simultaneous positions, equal-weight per slot
  - No overlapping capital: allocated cash is reserved from the moment of signal

Run: python3 backtest_option_a.py
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")

import sys
import time
import math
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, date, timedelta
from collections import defaultdict

# ── Period ──
DATA_START      = "2016-01-01"  # warmup for SMA224
BACKTEST_START  = "2018-01-01"
TODAY           = "2026-06-04"

# ── Universe ──
KOSPI_TOP_N     = 150
KOSDAQ_TOP_N    = 75
MIN_ROWS        = 280            # minimum rows for SMA224 computation

# ── Portfolio ──
INITIAL_CAPITAL = 5_000_000     # ₩5M
MAX_POSITIONS   = 3
MIN_ALLOCATION  = 200_000       # minimum trade size ₩200K
ROUND_TRIP_COST = 0.0035

# ── Exit policy B (Phase 1.5 best) and D ──
POLICIES = {
    "B": (-0.04, +0.08, +0.05, 0.025),   # stop, take, trail_start, trail_gap
    "D": (-0.05, +0.10, +0.05, 0.030),
}
DEFAULT_POLICY = "B"
MAX_HOLD_DAYS  = 5


# ════════════════════════════════════════════════════════
# DATA LOADING
# ════════════════════════════════════════════════════════

def _top_tickers(market: str, n: int) -> list[str]:
    try:
        df = fdr.StockListing(market)
        for mc in ["Marcap", "MarCap", "시가총액"]:
            if mc in df.columns:
                df = df.sort_values(mc, ascending=False)
                break
        for cc in ["Code", "Symbol", "종목코드"]:
            if cc in df.columns:
                return df[cc].head(n).tolist()
    except Exception as e:
        print(f"  Warning: {market} listing failed: {e}")
    return []


def load_data(tickers: list[str]):
    """Download OHLCV for all tickers + KOSPI index. Return stocks dict + regime series."""
    print("  Loading KOSPI index (KS11)...")
    try:
        ki = fdr.DataReader("KS11", DATA_START, TODAY)
        ki.index = pd.to_datetime(ki.index)
        r20 = ki["Close"].pct_change(20)
        r60 = ki["Close"].pct_change(60)

        def _regime(r20v, r60v):
            if pd.isna(r20v): return "NEUTRAL"
            if r20v <= -0.08: return "CRASH"
            if r20v < -0.03:  return "WEAK"
            if r20v > 0.03 and (pd.isna(r60v) or r60v > 0.02): return "BULL"
            return "NEUTRAL"

        regime_series = pd.Series(
            [_regime(r20.iloc[i], r60.iloc[i]) for i in range(len(ki))],
            index=ki.index
        )
        kospi_r20 = r20
        kospi_r60 = r60
        print(f"  KS11: {len(ki)} rows | "
              f"BULL={regime_series.eq('BULL').sum()} NEUTRAL={regime_series.eq('NEUTRAL').sum()} "
              f"WEAK={regime_series.eq('WEAK').sum()} CRASH={regime_series.eq('CRASH').sum()}")
    except Exception as e:
        print(f"  KS11 failed: {e}")
        regime_series = None
        kospi_r20 = kospi_r60 = None

    stocks = {}
    skipped = 0
    n = len(tickers)
    for i, t in enumerate(tickers):
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{n}] {len(stocks)} loaded...")
        try:
            df = fdr.DataReader(t, DATA_START, TODAY)
            df.index = pd.to_datetime(df.index)
            needed = ["Open", "High", "Low", "Close", "Volume"]
            if not all(c in df.columns for c in needed):
                skipped += 1; continue
            df = df[needed].dropna(subset=["Close"])
            if len(df) < MIN_ROWS:
                skipped += 1; continue

            c, h, lo, v = df["Close"], df["High"], df["Low"], df["Volume"]

            df["sma5"]   = c.rolling(5).mean()
            df["sma20"]  = c.rolling(20).mean()
            df["sma60"]  = c.rolling(60).mean()
            df["sma224"] = c.rolling(224).mean()

            df["sma20_slope"] = df["sma20"] - df["sma20"].shift(1)
            df["sma60_slope"] = df["sma60"] - df["sma60"].shift(1)

            df["return5"]  = c.pct_change(5)
            df["return20"] = c.pct_change(20)
            df["return60"] = c.pct_change(60)

            gain = c.diff().clip(lower=0).ewm(com=13, adjust=False).mean()
            loss = (-c.diff()).clip(lower=0).ewm(com=13, adjust=False).mean()
            df["rsi14"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

            tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
            df["atr14"]  = tr.ewm(com=13, adjust=False).mean()
            df["atr_pct"] = df["atr14"] / c

            df["avg_volume20"] = v.rolling(20).mean()
            df["volume_ratio"] = v / df["avg_volume20"].replace(0, np.nan)
            df["high20"] = h.rolling(20).max()
            df["low20"]  = lo.rolling(20).min()

            if regime_series is not None:
                df["regime"] = regime_series.reindex(df.index, method="ffill").fillna("NEUTRAL")
            else:
                df["regime"] = "NEUTRAL"

            if kospi_r20 is not None:
                df["rs20"] = c.pct_change(20) - kospi_r20.reindex(df.index, method="ffill")
                df["rs60"] = c.pct_change(60) - kospi_r60.reindex(df.index, method="ffill")
            else:
                df["rs20"] = df["rs60"] = np.nan

            stocks[t] = df
        except Exception:
            skipped += 1

    print(f"  Done: {len(stocks)} stocks, {skipped} skipped")
    return stocks


# ════════════════════════════════════════════════════════
# SIGNAL FUNCTIONS
# ════════════════════════════════════════════════════════

_NAN_CHECK = ["sma5","sma20","sma60","sma224","rsi14","atr_pct",
              "volume_ratio","return5","return20","high20","low20"]


def _base_ok(row) -> bool:
    return not any(pd.isna(row.get(c, np.nan)) for c in _NAN_CHECK)


def sig_s2_pullback(row, _=None) -> bool:
    """S2: SMA224-gated PULLBACK (best from Phase 1.5)."""
    try:
        close = row["Close"]
        return (
            row["sma224"] > 0 and close > row["sma224"]
            and close > row["sma20"]
            and close >= row["sma5"] * 0.995
            and row["sma20"] >= row["sma60"] * 0.98
            and -0.08 <= row["return5"] <= 0.06
            and -0.05 <= row["return20"] <= 0.35
            and 42 <= row["rsi14"] <= 64
            and row["atr_pct"] <= 0.08
            and row["volume_ratio"] >= 0.65
        )
    except Exception: return False


def sig_pullback_only(row, _=None) -> bool:
    """Pure PULLBACK: confirmed trend + controlled pullback."""
    try:
        return (
            row["return20"] > 0.01
            and row["sma20_slope"] > 0
            and row["sma60_slope"] > 0
            and row["Close"] > row["sma60"]
            and -0.05 <= row["return5"] <= -0.005
            and row["Close"] > row["low20"] * 1.01
            and 40 <= row["rsi14"] <= 68
            and row["atr_pct"] <= 0.12
        )
    except Exception: return False


def sig_breakout_only(row, _=None) -> bool:
    """Pure BREAKOUT: near 20d high, volume surge."""
    try:
        return (
            row["high20"] > 0
            and row["Close"] >= row["high20"] * 0.995
            and row["volume_ratio"] >= 1.5
            and row["rsi14"] <= 75
            and row["return20"] <= 0.40
        )
    except Exception: return False


SIGNAL_FUNCS = {
    "S2_SMA224_Pullback": sig_s2_pullback,
    "PullbackOnly":       sig_pullback_only,
    "BreakoutOnly":       sig_breakout_only,
}


# ════════════════════════════════════════════════════════
# CANDIDATE TRADE GENERATOR (per-stock, sequential)
# ════════════════════════════════════════════════════════

def _exit_sim(entry_price: float, future_rows: pd.DataFrame,
              stop: float, take: float, ts: float, tg: float,
              max_hold: int) -> tuple[float, int, str]:
    """Simulate a single trade. Returns (net_pnl, hold_days, exit_reason)."""
    peak = entry_price
    trailing = False
    stop_p  = entry_price * (1 + stop)
    take_p  = entry_price * (1 + take)
    trail_s = entry_price * (1 + ts)

    for i, (_, bar) in enumerate(future_rows.iterrows()):
        if i >= max_hold:
            ep = float(bar["Close"])
            return (ep / entry_price - 1) - ROUND_TRIP_COST, i + 1, "time"

        op  = float(bar["Open"])
        lo  = float(bar["Low"])
        hi  = float(bar["High"])
        cl  = float(bar["Close"])

        # Gap-through stop (open is below stop)
        if op <= stop_p:
            return (op / entry_price - 1) - ROUND_TRIP_COST, i + 1, "stop_gap"

        # Intraday stop wins over take
        if lo <= stop_p:
            return stop - ROUND_TRIP_COST, i + 1, "stop"
        if hi >= take_p:
            return take - ROUND_TRIP_COST, i + 1, "take"

        peak = max(peak, hi)
        if peak >= trail_s:
            trailing = True
        if trailing and lo <= peak * (1 - tg):
            ep = peak * (1 - tg)
            return (ep / entry_price - 1) - ROUND_TRIP_COST, i + 1, "trail"

    if len(future_rows) == 0:
        return -ROUND_TRIP_COST, 0, "time"
    ep = float(future_rows.iloc[-1]["Close"])
    return (ep / entry_price - 1) - ROUND_TRIP_COST, len(future_rows), "time"


def generate_trades(ticker: str, df: pd.DataFrame,
                    sig_fn, policy: str, max_hold: int) -> list[dict]:
    """Generate all non-overlapping candidate trades for one stock."""
    bt = df[df.index >= BACKTEST_START].copy()
    if len(bt) < 5: return []

    stop, take, trs, trg = POLICIES[policy]
    trades = []
    pos_end = -1

    for i in range(len(bt) - 1):
        if i <= pos_end: continue
        row = bt.iloc[i]
        if not _base_ok(row): continue
        if not sig_fn(row): continue

        # Determine strategy type for this signal
        if sig_fn is sig_breakout_only:
            strat = "BREAKOUT"
        elif sig_fn is sig_pullback_only:
            strat = "PULLBACK"
        else:
            # S2: classify by which sub-condition fired
            close = float(row["Close"])
            h20   = float(row.get("high20", 0))
            vr    = float(row.get("volume_ratio", 0))
            r5    = float(row.get("return5", 0))
            cbd   = int(row.get("consecutive_buy_days", 0))
            if h20 > 0 and close >= h20 * 0.995 and vr >= 1.5:
                strat = "BREAKOUT"
            elif r5 < -0.01:
                strat = "PULLBACK"
            else:
                strat = "PULLBACK"

        nxt = bt.iloc[i + 1]
        entry_price = float(nxt["Open"])
        if pd.isna(entry_price) or entry_price <= 0: continue

        future_end = min(i + 1 + max_hold + 1, len(bt))
        future = bt.iloc[i + 1: future_end]
        net_pnl, hold_d, reason = _exit_sim(entry_price, future, stop, take, trs, trg, max_hold)

        exit_idx = (i + 1) + hold_d - 1
        pos_end  = exit_idx
        exit_date = bt.index[min(exit_idx, len(bt) - 1)]

        trades.append({
            "ticker":      ticker,
            "signal_date": bt.index[i],
            "entry_date":  bt.index[i + 1],
            "exit_date":   exit_date,
            "entry_price": entry_price,
            "net_pnl":     net_pnl,
            "hold_days":   hold_d,
            "exit_reason": reason,
            "regime":      str(row.get("regime", "NEUTRAL")),
            "year":        bt.index[i].year,
            "strategy":    strat,
            "score_proxy": float(row.get("volume_ratio", 1.0)) * (1 + max(float(row.get("rs20", 0)), 0)),
        })

    return trades


# ════════════════════════════════════════════════════════
# PORTFOLIO SIMULATION
# ════════════════════════════════════════════════════════

def portfolio_simulate(all_trades: list[dict],
                       initial_capital: float,
                       max_positions: int) -> tuple[list[dict], list[dict]]:
    """
    Simulate portfolio with capital and position constraints.
    Returns (executed_trades, nav_events).
    nav_events = list of {date, nav} at each trade entry/exit.
    """
    # Sort candidates by entry_date, then by score (descending) for same-day priority
    candidates = sorted(all_trades, key=lambda t: (t["entry_date"], -t["score_proxy"]))

    cash = float(initial_capital)
    # open_positions: list of dicts with exit_date and allocated capital
    open_pos: list[dict] = []
    executed: list[dict] = []
    nav_events: list[dict] = []

    def _close_expired(before_date):
        nonlocal cash
        still_open = []
        for pos in open_pos:
            if pos["exit_date"] <= before_date:
                returned = pos["allocated"] * (1 + pos["net_pnl"])
                cash += returned
                executed.append({**pos, "capital_returned": returned})
                nav_events.append({"date": pos["exit_date"], "event": "exit",
                                   "cash": cash, "n_open": len(still_open)})
            else:
                still_open.append(pos)
        open_pos[:] = still_open

    for cand in candidates:
        entry_date = cand["entry_date"]

        # Close any positions that have already exited
        _close_expired(entry_date)

        # Check if we can enter
        if len(open_pos) >= max_positions:
            continue

        available_slots = max_positions - len(open_pos)
        allocation = cash / max_positions  # equal-weight per max slot
        allocation = min(allocation, cash)

        if allocation < MIN_ALLOCATION:
            continue

        # Enter
        cash -= allocation
        open_pos.append({
            **cand,
            "allocated": allocation,
        })
        nav_events.append({"date": entry_date, "event": "entry",
                           "cash": cash, "n_open": len(open_pos)})

    # Close all remaining positions at end of simulation
    _close_expired(datetime(2099, 1, 1))

    return executed, nav_events


# ════════════════════════════════════════════════════════
# PERFORMANCE METRICS
# ════════════════════════════════════════════════════════

def compute_portfolio_metrics(executed: list[dict],
                              initial_capital: float,
                              start_str: str, end_str: str) -> dict:
    if not executed:
        return {"n": 0, "cagr": 0, "mdd": 0, "pf": 0, "wr": 0}

    df = pd.DataFrame(executed)

    # Portfolio-weighted PnL (weighted by capital allocated)
    total_capital_deployed = df["allocated"].sum()
    weighted_pnl = (df["net_pnl"] * df["allocated"]).sum()
    if total_capital_deployed > 0:
        portfolio_avg_pnl = weighted_pnl / total_capital_deployed
    else:
        portfolio_avg_pnl = 0

    # Profit factor on portfolio-weighted trades
    wins   = df[df["net_pnl"] > 0]
    losses = df[df["net_pnl"] <= 0]
    win_capital  = (wins["net_pnl"] * wins["allocated"]).sum()
    loss_capital = abs((losses["net_pnl"] * losses["allocated"]).sum())
    pf = win_capital / loss_capital if loss_capital > 0 else float("inf")

    # Final NAV approximation from executed trades
    final_nav = initial_capital
    for _, row in df.iterrows():
        final_nav += row["allocated"] * row["net_pnl"]

    # CAGR
    years = (datetime.strptime(end_str, "%Y-%m-%d") -
             datetime.strptime(start_str, "%Y-%m-%d")).days / 365.25
    cagr = (final_nav / initial_capital) ** (1 / years) - 1 if years > 0 else 0

    # Approximate max drawdown from cumulative capital curve
    df_sorted = df.sort_values("exit_date")
    cumulative = initial_capital + (df_sorted["net_pnl"] * df_sorted["allocated"]).cumsum()
    peak_series = cumulative.cummax()
    dd_series   = (cumulative - peak_series) / peak_series
    mdd = dd_series.min() if len(dd_series) > 0 else 0

    # Yearly breakdown
    yearly = {}
    for yr, grp in df.groupby("year"):
        w = grp[grp["net_pnl"] > 0]
        l = grp[grp["net_pnl"] <= 0]
        w_cap = (w["net_pnl"] * w["allocated"]).sum()
        l_cap = abs((l["net_pnl"] * l["allocated"]).sum())
        yp = w_cap / l_cap if l_cap > 0 else float("inf")
        yearly[yr] = {
            "n": len(grp),
            "pf": yp,
            "wr": len(w) / len(grp),
            "avg_pnl": grp["net_pnl"].mean(),
        }

    # Regime breakdown
    regime_stats = {}
    for reg, grp in df.groupby("regime"):
        w = grp[grp["net_pnl"] > 0]
        l = grp[grp["net_pnl"] <= 0]
        w_cap = (w["net_pnl"] * w["allocated"]).sum()
        l_cap = abs((l["net_pnl"] * l["allocated"]).sum())
        reg_pf = w_cap / l_cap if l_cap > 0 else float("inf")
        regime_stats[reg] = {
            "n": len(grp),
            "pf": reg_pf,
            "wr": len(w) / len(grp),
        }

    # Strategy breakdown
    strategy_stats = {}
    for strat, grp in df.groupby("strategy"):
        w = grp[grp["net_pnl"] > 0]
        l = grp[grp["net_pnl"] <= 0]
        w_cap = (w["net_pnl"] * w["allocated"]).sum()
        l_cap = abs((l["net_pnl"] * l["allocated"]).sum())
        st_pf = w_cap / l_cap if l_cap > 0 else float("inf")
        strategy_stats[strat] = {
            "n": len(grp),
            "pf": st_pf,
            "wr": len(w) / len(grp),
        }

    return {
        "n":            len(df),
        "cagr":         cagr,
        "final_nav":    final_nav,
        "mdd":          mdd,
        "pf":           pf,
        "wr":           len(wins) / len(df),
        "avg_pnl":      df["net_pnl"].mean(),
        "port_avg_pnl": portfolio_avg_pnl,
        "stop_r":       (df["exit_reason"].str.startswith("stop")).mean(),
        "take_r":       (df["exit_reason"] == "take").mean(),
        "trail_r":      (df["exit_reason"] == "trail").mean(),
        "gap_r":        (df["exit_reason"] == "stop_gap").mean(),
        "yearly":       yearly,
        "regime":       regime_stats,
        "strategy":     strategy_stats,
    }


# ════════════════════════════════════════════════════════
# REPORT
# ════════════════════════════════════════════════════════

def fp(v, d=2): return f"{v*100:{'+' if v>=0 else ''}.{d}f}%"
def pft(pf): return " <<PF>1.10" if pf >= 1.10 else (" <PF>=1.00" if pf >= 1.00 else "")


def print_report(label: str, m: dict):
    print(f"\n{'─'*65}")
    print(f"  {label}")
    print(f"{'─'*65}")
    print(f"  Trades executed (portfolio-constrained) : {m['n']}")
    print(f"  Final NAV                               : ₩{m['final_nav']:,.0f}")
    print(f"  CAGR ({BACKTEST_START}→{TODAY})        : {fp(m['cagr'])}")
    print(f"  Max Drawdown (approx, from exit events) : {fp(m['mdd'])}")
    print(f"  Portfolio-weighted Profit Factor        : {m['pf']:.2f}{pft(m['pf'])}")
    print(f"  Portfolio-weighted avg PnL/trade        : {fp(m['port_avg_pnl'])}")
    print(f"  Win Rate                                : {m['wr']*100:.1f}%")
    print(f"  Stop rate / Take / Trail / Gap          : "
          f"{m['stop_r']*100:.1f}% / {m['take_r']*100:.1f}% / "
          f"{m['trail_r']*100:.1f}% / {m['gap_r']*100:.1f}%")

    if m.get("regime"):
        print(f"\n  Regime breakdown:")
        print(f"  {'Regime':<10} {'N':>5} {'PF':>6} {'WR%':>6}")
        for reg in ["BULL","NEUTRAL","WEAK","CRASH"]:
            r = m["regime"].get(reg)
            if r:
                print(f"  {reg:<10} {r['n']:>5} {r['pf']:>6.2f}{pft(r['pf'])} {r['wr']*100:>5.1f}%")

    if m.get("strategy"):
        print(f"\n  Strategy breakdown:")
        print(f"  {'Strategy':<12} {'N':>5} {'PF':>6} {'WR%':>6}")
        for strat, r in sorted(m["strategy"].items()):
            print(f"  {strat:<12} {r['n']:>5} {r['pf']:>6.2f}{pft(r['pf'])} {r['wr']*100:>5.1f}%")

    if m.get("yearly"):
        print(f"\n  Yearly breakdown:")
        print(f"  {'Year':<6} {'N':>5} {'PF':>6} {'WR%':>6} {'AvgPnL':>8}")
        for yr in sorted(m["yearly"].keys()):
            y = m["yearly"][yr]
            print(f"  {yr:<6} {y['n']:>5} {y['pf']:>6.2f}{pft(y['pf'])} {y['wr']*100:>5.1f}% {fp(y['avg_pnl']):>8}")


# ════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════

def main():
    t0 = time.time()
    print("=" * 65)
    print("OPTION A — EXTENDED PORTFOLIO BACKTEST")
    print(f"Period  : {BACKTEST_START} → {TODAY}")
    print(f"Universe: KOSPI top {KOSPI_TOP_N} + KOSDAQ top {KOSDAQ_TOP_N}")
    print(f"Capital : ₩{INITIAL_CAPITAL:,} | Max positions: {MAX_POSITIONS}")
    print("=" * 65)
    print("\n[SURVIVORSHIP BIAS] Universe uses current (2026) market-cap ranking.")
    print("  Delisted/demoted stocks from 2018-2026 are absent. Results are an upper bound.\n")

    # Load universe
    print("[1] Loading universe...")
    kospi_tickers  = _top_tickers("KOSPI",  KOSPI_TOP_N)
    kosdaq_tickers = _top_tickers("KOSDAQ", KOSDAQ_TOP_N)
    tickers = list(dict.fromkeys(kospi_tickers + kosdaq_tickers))
    print(f"  {len(tickers)} tickers")

    # Download data
    print("[2] Downloading 2016-2026 data...")
    stocks = load_data(tickers)
    print(f"  {len(stocks)} stocks loaded in {time.time()-t0:.0f}s")

    # Generate all candidate trades for each strategy
    print("\n[3] Generating candidate trades...")
    results = {}

    for sig_name, sig_fn in SIGNAL_FUNCS.items():
        all_cands = []
        for ticker, df in stocks.items():
            trades = generate_trades(ticker, df, sig_fn, DEFAULT_POLICY, MAX_HOLD_DAYS)
            all_cands.extend(trades)
        print(f"  {sig_name}: {len(all_cands)} candidate trades (unconstrained)")
        results[sig_name] = all_cands

    # Portfolio simulation for each strategy
    print("\n[4] Running portfolio simulation (₩5M, max 3 positions)...")
    for sig_name, cands in results.items():
        exec_trades, nav_events = portfolio_simulate(cands, INITIAL_CAPITAL, MAX_POSITIONS)
        m = compute_portfolio_metrics(exec_trades, INITIAL_CAPITAL, BACKTEST_START, TODAY)
        print_report(f"{sig_name}  [Policy {DEFAULT_POLICY}, Hold {MAX_HOLD_DAYS}d]", m)

    # Also test S2 with Policy D
    print("\n[5] S2_SMA224 with Policy D (-5%/+10%)...")
    policy_d_cands = []
    for ticker, df in stocks.items():
        trades = generate_trades(ticker, df, sig_s2_pullback, "D", MAX_HOLD_DAYS)
        policy_d_cands.extend(trades)
    exec_d, _ = portfolio_simulate(policy_d_cands, INITIAL_CAPITAL, MAX_POSITIONS)
    m_d = compute_portfolio_metrics(exec_d, INITIAL_CAPITAL, BACKTEST_START, TODAY)
    print_report("S2_SMA224  [Policy D, Hold 5d]", m_d)

    # S2 with 10d hold
    print("\n[6] S2_SMA224 with 10-day hold...")
    cands_10d = []
    for ticker, df in stocks.items():
        trades = generate_trades(ticker, df, sig_s2_pullback, DEFAULT_POLICY, 10)
        cands_10d.extend(trades)
    exec_10, _ = portfolio_simulate(cands_10d, INITIAL_CAPITAL, MAX_POSITIONS)
    m_10 = compute_portfolio_metrics(exec_10, INITIAL_CAPITAL, BACKTEST_START, TODAY)
    print_report("S2_SMA224  [Policy B, Hold 10d]", m_10)

    # BULL-only regime gate for best strategy
    print("\n[7] S2_SMA224 BULL-only regime gate...")
    bull_cands = [t for t in results["S2_SMA224_Pullback"] if t["regime"] == "BULL"]
    exec_bull, _ = portfolio_simulate(bull_cands, INITIAL_CAPITAL, MAX_POSITIONS)
    m_bull = compute_portfolio_metrics(exec_bull, INITIAL_CAPITAL, BACKTEST_START, TODAY)
    print_report("S2_SMA224  [BULL only, Policy B, Hold 5d]", m_bull)

    # Summary
    elapsed = time.time() - t0
    print(f"\n{'='*65}")
    print(f"OPTION A COMPLETE — Runtime: {elapsed:.0f}s")
    print(f"{'='*65}")
    print(f"\nAudit notes:")
    print(f"  - Regime: KOSPI 20d/60d pct_change as of signal day close (no lookahead)")
    print(f"  - Entry: next-day open (signal-day conditions evaluated at close)")
    print(f"  - Gap-through stop: open <= stop_price → exit at open price")
    print(f"  - Same-day conflict: intraday low <= stop wins over high >= take")
    print(f"  - MDD computed from cumulative exit events (not intraday marks)")
    print(f"  - Cost: {ROUND_TRIP_COST*100:.2f}% round-trip applied at exit")


if __name__ == "__main__":
    main()
