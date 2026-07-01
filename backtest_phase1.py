"""
backtest_phase1.py — KR Bot Strategy Redesign Phase 1
======================================================
Compares 3 entry strategies × 4 exit policies across KOSPI/KOSDAQ top stocks.
Run: python3 backtest_phase1.py
"""

import warnings
warnings.filterwarnings("ignore")

import sys
import time
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TODAY = datetime(2026, 6, 4)
BACKTEST_START = TODAY - timedelta(days=365 * 2 + 10)  # ~2 years back
DATA_START = datetime(2023, 1, 1)                        # warmup for sma224
BACKTEST_START_STR = BACKTEST_START.strftime("%Y-%m-%d")
DATA_START_STR = DATA_START.strftime("%Y-%m-%d")
TODAY_STR = TODAY.strftime("%Y-%m-%d")

KOSPI_TOP_N = 100
KOSDAQ_TOP_N = 50
MIN_ROWS = 270
ROUND_TRIP_COST = 0.0035

MAX_HOLD_DAYS = 5


# ─────────────────────────────────────────────
# INDICATOR COMPUTATION
# ─────────────────────────────────────────────
def compute_indicators(df, kospi_df):
    """Compute all required indicators for a stock dataframe."""
    df = df.copy()
    c = df["Close"]
    h = df["High"]
    lo = df["Low"]
    v = df["Volume"]

    df["sma5"]   = c.rolling(5).mean()
    df["sma20"]  = c.rolling(20).mean()
    df["sma60"]  = c.rolling(60).mean()
    df["sma120"] = c.rolling(120).mean()
    df["sma224"] = c.rolling(224).mean()

    df["sma20_prev"] = df["sma20"].shift(1)
    df["sma60_prev"] = df["sma60"].shift(1)
    df["sma20_slope"] = df["sma20"] - df["sma20_prev"]
    df["sma60_slope"] = df["sma60"] - df["sma60_prev"]

    df["return5"]  = c.pct_change(5)
    df["return20"] = c.pct_change(20)
    df["return60"] = c.pct_change(60)

    # RSI 14
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    # ATR 14
    hl = h - lo
    hc = (h - c.shift(1)).abs()
    lc = (lo - c.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(com=13, adjust=False).mean()
    df["atr_pct"] = df["atr14"] / c

    # Volume
    df["avg_volume20"]  = v.rolling(20).mean()
    df["volume_ratio"]  = v / df["avg_volume20"].replace(0, np.nan)

    # 20-day high/low
    df["high20"] = h.rolling(20).max()
    df["low20"]  = lo.rolling(20).min()

    # Relative strength vs KOSPI
    # Align on dates
    if kospi_df is not None and len(kospi_df) > 0:
        kospi_ret20 = kospi_df["Close"].pct_change(20).rename("kospi_return20")
        kospi_ret60 = kospi_df["Close"].pct_change(60).rename("kospi_return60")
        df = df.join(kospi_ret20, how="left")
        df = df.join(kospi_ret60, how="left")
        df["relative_strength20"] = df["return20"] - df["kospi_return20"]
        df["relative_strength60"] = df["return60"] - df["kospi_return60"]
    else:
        df["kospi_return20"] = np.nan
        df["kospi_return60"] = np.nan
        df["relative_strength20"] = np.nan
        df["relative_strength60"] = np.nan

    return df


# ─────────────────────────────────────────────
# ENTRY SIGNAL GENERATORS
# ─────────────────────────────────────────────
def signal_strategy1(row):
    """Current DEFENSE_LONG (baseline)."""
    try:
        return (
            row["Close"] > row["sma20"]
            and row["Close"] >= row["sma5"] * 0.995
            and row["sma20"] >= row["sma60"] * 0.98
            and -0.08 <= row["return5"] <= 0.06
            and -0.05 <= row["return20"] <= 0.35
            and 42 <= row["rsi14"] <= 64
            and row["atr_pct"] <= 0.08
            and row["volume_ratio"] >= 0.65
        )
    except Exception:
        return False


def signal_strategy2(row):
    """SMA224-gated."""
    try:
        if not (row["sma224"] > 0 and row["Close"] > row["sma224"]):
            return False
        return signal_strategy1(row)
    except Exception:
        return False


def signal_strategy3(row):
    """Trend-slope + Relative-strength Pullback."""
    try:
        return (
            # Confirmed uptrend
            row["return20"] > 0.01
            and row["return60"] > 0.02
            and row["sma20_slope"] > 0
            and row["sma60_slope"] > 0
            and row["Close"] > row["sma60"]
            and row["relative_strength20"] > 0.005
            # Controlled pullback
            and -0.05 <= row["return5"] <= -0.005
            and row["Close"] >= row["low20"] * 1.01
            and 0.4 <= row["volume_ratio"] <= 1.5
            and 40 <= row["rsi14"] <= 65
            # Quality gate
            and row["atr_pct"] <= 0.12
        )
    except Exception:
        return False


SIGNAL_FUNCS = {
    "S1_Current": signal_strategy1,
    "S2_SMA224": signal_strategy2,
    "S3_TrendPullback": signal_strategy3,
}


# ─────────────────────────────────────────────
# EXIT POLICY FACTORIES
# ─────────────────────────────────────────────
def make_exit_params(policy_name, atr_pct_signal=None):
    """Return (stop, take, trail_start, trail_gap) for a given policy."""
    if policy_name == "A":
        return -0.03, 0.07, 0.04, 0.02
    elif policy_name == "B":
        return -0.04, 0.08, 0.05, 0.025
    elif policy_name == "C":
        atr = atr_pct_signal if atr_pct_signal and not np.isnan(atr_pct_signal) else 0.04
        stop = -max(0.025, min(0.06, 1.5 * atr))
        take = abs(stop) * 2.2
        trail_start = abs(stop)
        trail_gap = atr * 0.8
        return stop, take, trail_start, trail_gap
    elif policy_name == "D":
        return -0.05, 0.10, 0.05, 0.03
    else:
        raise ValueError(f"Unknown policy: {policy_name}")


# ─────────────────────────────────────────────
# TRADE SIMULATOR
# ─────────────────────────────────────────────
def simulate_trade(entry_price, future_bars, stop, take, trail_start, trail_gap, max_hold):
    """
    Simulate a single trade given entry price and subsequent OHLC bars.
    Returns: (net_pnl_pct, holding_days, exit_reason)
    exit_reason: 'stop', 'take', 'trail', 'time'
    """
    peak = entry_price
    trailing_active = False

    for i, (_, bar) in enumerate(future_bars.iterrows()):
        if i >= max_hold:
            # Time exit at close of last bar
            exit_price = bar["Close"]
            gross = (exit_price - entry_price) / entry_price
            net = gross - ROUND_TRIP_COST
            return net, i + 1, "time"

        day_low  = bar["Low"]
        day_high = bar["High"]
        day_close = bar["Close"]

        gross_low  = (day_low - entry_price) / entry_price
        gross_high = (day_high - entry_price) / entry_price

        # Check stop first (intraday worst case)
        if gross_low <= stop:
            exit_price = entry_price * (1 + stop)
            net = stop - ROUND_TRIP_COST
            return net, i + 1, "stop"

        # Check take
        if gross_high >= take:
            exit_price = entry_price * (1 + take)
            net = take - ROUND_TRIP_COST
            return net, i + 1, "take"

        # Update trailing
        if day_high > peak:
            peak = day_high

        gross_peak = (peak - entry_price) / entry_price
        if gross_peak >= trail_start:
            trailing_active = True

        if trailing_active:
            drop_from_peak = (peak - day_low) / peak
            if drop_from_peak >= trail_gap:
                exit_price = peak * (1 - trail_gap)
                gross = (exit_price - entry_price) / entry_price
                net = gross - ROUND_TRIP_COST
                return net, i + 1, "trail"

    # Exhausted future_bars — exit at last close
    if len(future_bars) == 0:
        return -ROUND_TRIP_COST, 0, "time"
    exit_price = future_bars.iloc[-1]["Close"]
    gross = (exit_price - entry_price) / entry_price
    net = gross - ROUND_TRIP_COST
    return net, len(future_bars), "time"


# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
def run_backtest(stocks_data, strategy_name, exit_policy, max_hold=MAX_HOLD_DAYS):
    """
    Run backtest for one strategy × one exit policy.
    Returns list of trade dicts.
    """
    signal_fn = SIGNAL_FUNCS[strategy_name]
    trades = []

    for ticker, df in stocks_data.items():
        # Filter to backtest window
        bt_df = df[df.index >= BACKTEST_START_STR].copy()
        if len(bt_df) < 5:
            continue

        in_position = False
        position_end_idx = -1

        for i in range(len(bt_df) - 1):  # need i+1 for next-day open
            if i <= position_end_idx:
                continue  # still in previous position

            row = bt_df.iloc[i]

            # Skip NaN rows
            key_cols = ["sma5", "sma20", "sma60", "sma224", "rsi14", "atr_pct",
                        "volume_ratio", "return5", "return20", "return60",
                        "sma20_slope", "sma60_slope", "low20", "relative_strength20"]
            if any(pd.isna(row.get(c, np.nan)) for c in key_cols):
                continue

            # Check entry signal
            if not signal_fn(row):
                continue

            # Entry at next-day open
            next_bar = bt_df.iloc[i + 1]
            entry_price = next_bar["Open"]
            if pd.isna(entry_price) or entry_price <= 0:
                continue

            # Get exit params
            atr_val = row["atr_pct"] if not pd.isna(row["atr_pct"]) else None
            stop, take, trail_start, trail_gap = make_exit_params(exit_policy, atr_val)

            # Future bars for exit simulation (from i+1 onwards)
            future_start = i + 1
            future_end = min(future_start + max_hold + 1, len(bt_df))
            future_bars = bt_df.iloc[future_start:future_end]

            net_pnl, hold_days, exit_reason = simulate_trade(
                entry_price, future_bars, stop, take, trail_start, trail_gap, max_hold
            )

            position_end_idx = future_start + hold_days - 1

            trades.append({
                "ticker": ticker,
                "entry_date": bt_df.index[i],
                "exit_reason": exit_reason,
                "net_pnl": net_pnl,
                "hold_days": hold_days,
                "stop": stop,
                "take": take,
            })

    return trades


# ─────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────
def compute_metrics(trades):
    if not trades:
        return {
            "trade_count": 0, "win_rate": 0, "avg_net_pnl": 0,
            "profit_factor": 0, "stop_rate": 0, "take_rate": 0,
            "trail_rate": 0, "time_rate": 0,
            "avg_winner": 0, "avg_loser": 0, "avg_hold": 0,
            "max_drawdown_streak": 0,
        }

    df = pd.DataFrame(trades)
    n = len(df)
    wins = df[df["net_pnl"] > 0]["net_pnl"]
    losses = df[df["net_pnl"] <= 0]["net_pnl"]

    win_rate = len(wins) / n if n > 0 else 0
    avg_net = df["net_pnl"].mean()
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")

    stop_rate  = (df["exit_reason"] == "stop").mean()
    take_rate  = (df["exit_reason"] == "take").mean()
    trail_rate = (df["exit_reason"] == "trail").mean()
    time_rate  = (df["exit_reason"] == "time").mean()

    avg_winner = wins.mean() if len(wins) > 0 else 0
    avg_loser  = losses.mean() if len(losses) > 0 else 0
    avg_hold   = df["hold_days"].mean()

    # Max drawdown streak: max consecutive losses per ticker
    max_streak = 0
    for ticker, grp in df.groupby("ticker"):
        streak = 0
        best = 0
        for pnl in grp["net_pnl"]:
            if pnl <= 0:
                streak += 1
                best = max(best, streak)
            else:
                streak = 0
        max_streak = max(max_streak, best)

    return {
        "trade_count": n,
        "win_rate": win_rate,
        "avg_net_pnl": avg_net,
        "profit_factor": pf,
        "stop_rate": stop_rate,
        "take_rate": take_rate,
        "trail_rate": trail_rate,
        "time_rate": time_rate,
        "avg_winner": avg_winner,
        "avg_loser": avg_loser,
        "avg_hold": avg_hold,
        "max_drawdown_streak": max_streak,
    }


# ─────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────
def load_stock_universe():
    """Load top KOSPI + KOSDAQ stocks by market cap."""
    print("Loading KOSPI listing...")
    try:
        kospi = fdr.StockListing("KOSPI")
    except Exception as e:
        print(f"  Error loading KOSPI: {e}")
        kospi = pd.DataFrame()

    print("Loading KOSDAQ listing...")
    try:
        kosdaq = fdr.StockListing("KOSDAQ")
    except Exception as e:
        print(f"  Error loading KOSDAQ: {e}")
        kosdaq = pd.DataFrame()

    tickers = []

    # Find market cap column
    def get_top_by_mktcap(listing, n):
        if listing is None or len(listing) == 0:
            return []
        cols = listing.columns.tolist()
        mktcap_col = None
        for c in ["Marcap", "MarCap", "시가총액", "MktCap", "mktcap"]:
            if c in cols:
                mktcap_col = c
                break
        code_col = None
        for c in ["Code", "Symbol", "종목코드"]:
            if c in cols:
                code_col = c
                break
        if code_col is None:
            return []
        if mktcap_col:
            sub = listing[[code_col, mktcap_col]].dropna()
            sub = sub[sub[mktcap_col] > 0]
            sub = sub.sort_values(mktcap_col, ascending=False).head(n)
            return sub[code_col].tolist()
        else:
            return listing[code_col].head(n).tolist()

    kospi_tickers  = get_top_by_mktcap(kospi, KOSPI_TOP_N)
    kosdaq_tickers = get_top_by_mktcap(kosdaq, KOSDAQ_TOP_N)

    print(f"  KOSPI tickers: {len(kospi_tickers)}, KOSDAQ tickers: {len(kosdaq_tickers)}")
    return kospi_tickers + kosdaq_tickers


def load_all_data(tickers):
    """Download OHLCV for all tickers and compute indicators."""
    print(f"\nDownloading KOSPI index (KS11)...")
    try:
        kospi_df = fdr.DataReader("KS11", DATA_START_STR, TODAY_STR)
        kospi_df.index = pd.to_datetime(kospi_df.index)
        print(f"  KS11: {len(kospi_df)} rows")
    except Exception as e:
        print(f"  Warning: KS11 failed ({e}), using None")
        kospi_df = None

    stocks_data = {}
    skipped = 0
    n = len(tickers)

    print(f"\nDownloading {n} stocks...")
    for i, ticker in enumerate(tickers):
        if (i + 1) % 25 == 0 or i == 0:
            print(f"  [{i+1}/{n}] Processing {ticker}...")

        try:
            df = fdr.DataReader(ticker, DATA_START_STR, TODAY_STR)
            df.index = pd.to_datetime(df.index)

            # Ensure required columns
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                if col not in df.columns:
                    raise ValueError(f"Missing column {col}")

            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])

            if len(df) < MIN_ROWS:
                skipped += 1
                continue

            # Align kospi_df index to this stock's index
            if kospi_df is not None:
                aligned_kospi = kospi_df.reindex(df.index, method="ffill")
            else:
                aligned_kospi = None

            df = compute_indicators(df, aligned_kospi)
            stocks_data[ticker] = df

        except Exception as e:
            skipped += 1

    print(f"\nLoaded {len(stocks_data)} stocks, skipped {skipped}")
    return stocks_data


# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────
def fmt_pct(v, digits=2):
    return f"{v*100:+.{digits}f}%"

def fmt_pct_plain(v, digits=1):
    return f"{v*100:.{digits}f}%"

def fmt_float(v, digits=2):
    if v == float("inf"):
        return "  inf"
    return f"{v:.{digits}f}"


def print_entry_comparison(results_matrix, exit_policy="B", max_hold=5):
    header = f"\nENTRY STRATEGY COMPARISON (Exit Policy {exit_policy}, Hold {max_hold}d)"
    print(header)
    print("=" * len(header.strip()))
    print(f"{'Strategy':<28} {'Trades':>7} {'WinRate':>8} {'AvgNet':>8} {'ProfFact':>10} {'StopRate':>9} {'AvgWin':>8} {'AvgLoss':>8} {'AvgHold':>8}")
    print("-" * 100)

    strategy_labels = {
        "S1_Current":       "Current (DEFENSE_LONG)",
        "S2_SMA224":        "SMA224-gated",
        "S3_TrendPullback": "Trend+RelStr Pullback",
    }

    for skey in ["S1_Current", "S2_SMA224", "S3_TrendPullback"]:
        key = (skey, exit_policy, max_hold)
        if key not in results_matrix:
            continue
        m = results_matrix[key]
        label = strategy_labels.get(skey, skey)
        print(
            f"{label:<28} {m['trade_count']:>7} "
            f"{fmt_pct_plain(m['win_rate']):>8} "
            f"{fmt_pct(m['avg_net_pnl']):>8} "
            f"{fmt_float(m['profit_factor']):>10} "
            f"{fmt_pct_plain(m['stop_rate']):>9} "
            f"{fmt_pct(m['avg_winner']):>8} "
            f"{fmt_pct(m['avg_loser']):>8} "
            f"{m['avg_hold']:>8.1f}"
        )


def print_exit_comparison(results_matrix, strategy="S3_TrendPullback", max_hold=5):
    exit_labels = {"A": "-3%/+7%", "B": "-4%/+8%", "C": "ATR-aware", "D": "-5%/+10%"}
    header = f"\nEXIT POLICY COMPARISON (Strategy: {strategy}, Hold {max_hold}d)"
    print(header)
    print("=" * len(header.strip()))
    print(f"{'Policy':<10} {'StopTake':<12} {'Trades':>7} {'WinRate':>8} {'AvgNet':>8} {'ProfFact':>10} {'StopRate':>9} {'TakeRate':>9} {'TrailRate':>10} {'TimeRate':>9}")
    print("-" * 110)

    for pol in ["A", "B", "C", "D"]:
        key = (strategy, pol, max_hold)
        if key not in results_matrix:
            continue
        m = results_matrix[key]
        print(
            f"{pol:<10} {exit_labels.get(pol,'')::<12} {m['trade_count']:>7} "
            f"{fmt_pct_plain(m['win_rate']):>8} "
            f"{fmt_pct(m['avg_net_pnl']):>8} "
            f"{fmt_float(m['profit_factor']):>10} "
            f"{fmt_pct_plain(m['stop_rate']):>9} "
            f"{fmt_pct_plain(m['take_rate']):>9} "
            f"{fmt_pct_plain(m['trail_rate']):>10} "
            f"{fmt_pct_plain(m['time_rate']):>9}"
        )


def print_hold_sensitivity(results_matrix, strategy, exit_policy):
    header = f"\nHOLD PERIOD SENSITIVITY (Strategy: {strategy}, Exit: {exit_policy})"
    print(header)
    print("=" * len(header.strip()))
    print(f"{'Hold':>6} {'Trades':>7} {'WinRate':>8} {'AvgNet':>8} {'ProfFact':>10} {'StopRate':>9} {'AvgWin':>8} {'AvgLoss':>8}")
    print("-" * 75)

    for hold in [3, 5, 10]:
        key = (strategy, exit_policy, hold)
        if key not in results_matrix:
            continue
        m = results_matrix[key]
        print(
            f"{hold:>5}d {m['trade_count']:>7} "
            f"{fmt_pct_plain(m['win_rate']):>8} "
            f"{fmt_pct(m['avg_net_pnl']):>8} "
            f"{fmt_float(m['profit_factor']):>10} "
            f"{fmt_pct_plain(m['stop_rate']):>9} "
            f"{fmt_pct(m['avg_winner']):>8} "
            f"{fmt_pct(m['avg_loser']):>8}"
        )


def print_full_matrix(results_matrix):
    header = "\nFULL RESULTS MATRIX (All Strategy × Exit × Hold combinations)"
    print(header)
    print("=" * len(header.strip()))
    print(f"{'Strategy':<20} {'Exit':>6} {'Hold':>5} {'Trades':>7} {'WinRate':>8} {'AvgNet':>8} {'ProfFact':>10} {'StopRate':>9}")
    print("-" * 85)
    for (s, p, h), m in sorted(results_matrix.items()):
        print(
            f"{s:<20} {p:>6} {h:>5} {m['trade_count']:>7} "
            f"{fmt_pct_plain(m['win_rate']):>8} "
            f"{fmt_pct(m['avg_net_pnl']):>8} "
            f"{fmt_float(m['profit_factor']):>10} "
            f"{fmt_pct_plain(m['stop_rate']):>9}"
        )


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    t0 = time.time()
    print("=" * 60)
    print("KR BOT STRATEGY REDESIGN — PHASE 1 BACKTEST")
    print(f"Backtest window: {BACKTEST_START_STR} → {TODAY_STR}")
    print(f"Data warmup from: {DATA_START_STR}")
    print("=" * 60)

    # Load universe
    tickers = load_stock_universe()
    if not tickers:
        print("ERROR: No tickers loaded. Abort.")
        sys.exit(1)

    # Download & compute indicators
    stocks_data = load_all_data(tickers)
    if not stocks_data:
        print("ERROR: No stock data loaded. Abort.")
        sys.exit(1)

    print(f"\nData loading done in {time.time()-t0:.1f}s")

    # Build results matrix
    # For main 3x4 grid: use max_hold=5
    # For hold sensitivity: use best strategy+exit with hold 3,5,10
    strategies  = ["S1_Current", "S2_SMA224", "S3_TrendPullback"]
    exit_policies = ["A", "B", "C", "D"]
    hold_days_list = [5]  # main grid uses 5

    results_matrix = {}

    print("\nRunning backtests...")
    total_runs = len(strategies) * len(exit_policies)
    run_num = 0

    for s in strategies:
        for p in exit_policies:
            run_num += 1
            print(f"  [{run_num}/{total_runs}] Strategy={s}, Policy={p}, Hold=5 ...", end=" ", flush=True)
            t1 = time.time()
            trades = run_backtest(stocks_data, s, p, max_hold=5)
            m = compute_metrics(trades)
            results_matrix[(s, p, 5)] = m
            print(f"{m['trade_count']} trades, WinRate={fmt_pct_plain(m['win_rate'])}, AvgNet={fmt_pct(m['avg_net_pnl'])} ({time.time()-t1:.1f}s)")

    # Find best combo by profit_factor for hold sensitivity
    best_key = None
    best_pf = -1
    for (s, p, h), m in results_matrix.items():
        if m["profit_factor"] > best_pf and m["trade_count"] > 20:
            best_pf = m["profit_factor"]
            best_key = (s, p, h)

    best_strategy = best_key[0] if best_key else "S3_TrendPullback"
    best_policy   = best_key[1] if best_key else "B"
    print(f"\nBest combo so far: {best_strategy} × Policy {best_policy} (PF={best_pf:.2f})")

    # Hold sensitivity for best combo
    print(f"\nRunning hold sensitivity for {best_strategy} × Policy {best_policy}...")
    for hold in [3, 10]:
        print(f"  Hold={hold} ...", end=" ", flush=True)
        t1 = time.time()
        trades = run_backtest(stocks_data, best_strategy, best_policy, max_hold=hold)
        m = compute_metrics(trades)
        results_matrix[(best_strategy, best_policy, hold)] = m
        print(f"{m['trade_count']} trades, WinRate={fmt_pct_plain(m['win_rate'])}, AvgNet={fmt_pct(m['avg_net_pnl'])} ({time.time()-t1:.1f}s)")

    # Also run hold=3,10 for hold=5 already done
    # (hold=5 already in matrix)

    total_elapsed = time.time() - t0
    print(f"\nAll backtests done in {total_elapsed:.1f}s")

    # ── PRINT REPORTS ──
    print("\n")
    print("=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)

    # Entry comparison (Policy B, Hold 5)
    print_entry_comparison(results_matrix, exit_policy="B", max_hold=5)

    # Exit comparison (best entry strategy or S3, Hold 5)
    print_exit_comparison(results_matrix, strategy=best_strategy, max_hold=5)

    # Hold sensitivity (best strategy + best exit)
    print_hold_sensitivity(results_matrix, best_strategy, best_policy)

    # Full matrix
    print_full_matrix(results_matrix)

    # Summary recommendation
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if best_key:
        m = results_matrix[best_key]
        print(f"Best configuration: {best_strategy} × Policy {best_policy} × Hold 5d")
        print(f"  Trades: {m['trade_count']}")
        print(f"  Win Rate: {fmt_pct_plain(m['win_rate'])}")
        print(f"  Avg Net PnL: {fmt_pct(m['avg_net_pnl'])}")
        print(f"  Profit Factor: {fmt_float(m['profit_factor'])}")
        print(f"  Stop Rate: {fmt_pct_plain(m['stop_rate'])}")
        print(f"  Take Rate: {fmt_pct_plain(m['take_rate'])}")
        print(f"  Trail Rate: {fmt_pct_plain(m['trail_rate'])}")
        print(f"  Time-out Rate: {fmt_pct_plain(m['time_rate'])}")
        print(f"  Avg Winner: {fmt_pct(m['avg_winner'])}")
        print(f"  Avg Loser:  {fmt_pct(m['avg_loser'])}")
        print(f"  Avg Hold Days: {m['avg_hold']:.1f}")

    print(f"\nTotal runtime: {total_elapsed:.1f}s")


if __name__ == "__main__":
    main()
