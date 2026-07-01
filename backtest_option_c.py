"""
backtest_option_c.py — Option C: BREAKOUT Strategy Isolated
============================================================
Separates BREAKOUT from PULLBACK to test if high-quality breakout signals
can achieve PF > 1.10.

Per Codex decision doc, tests:
  - BREAKOUT vs PULLBACK side by side
  - BREAKOUT by market regime (BULL/NEUTRAL/WEAK/CRASH)
  - Quality filter combinations (candle body, close-near-high, volume, SMA224, RS)
  - Phase 1.5 finding: next-day open is best entry (T2/T3 confirmations hurt)
  - BREAKOUT-specific exits: wider trailing, no fixed take
  - Portfolio-level simulation (not just summed trade returns)

AUDIT: Same no-lookahead constraints as Option A.

Run: python3 backtest_option_c.py
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")

import time
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

DATA_START      = "2016-01-01"
BACKTEST_START  = "2018-01-01"
TODAY           = "2026-06-04"

KOSPI_TOP_N     = 150
KOSDAQ_TOP_N    = 75
MIN_ROWS        = 280
ROUND_TRIP_COST = 0.0035

INITIAL_CAPITAL = 5_000_000
MAX_POSITIONS   = 3
MIN_ALLOCATION  = 200_000


# ════════════════════════════════════════════════════════
# DATA LOADING (shared with Option A)
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
        print(f"  {market} listing failed: {e}")
    return []


def load_data(tickers: list[str]):
    print("  Loading KS11...")
    try:
        ki = fdr.DataReader("KS11", DATA_START, TODAY)
        ki.index = pd.to_datetime(ki.index)
        r20 = ki["Close"].pct_change(20)
        r60 = ki["Close"].pct_change(60)

        def _reg(r20v, r60v):
            if pd.isna(r20v): return "NEUTRAL"
            if r20v <= -0.08: return "CRASH"
            if r20v < -0.03:  return "WEAK"
            if r20v > 0.03 and (pd.isna(r60v) or r60v > 0.02): return "BULL"
            return "NEUTRAL"

        reg_series = pd.Series([_reg(r20.iloc[i], r60.iloc[i]) for i in range(len(ki))], index=ki.index)
        print(f"  KS11: {len(ki)} rows | {reg_series.value_counts().to_dict()}")
    except Exception as e:
        print(f"  KS11 failed: {e}")
        reg_series = r20 = r60 = None

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
            if not all(c in df.columns for c in needed): skipped += 1; continue
            df = df[needed].dropna(subset=["Close"])
            if len(df) < MIN_ROWS: skipped += 1; continue

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
            df["rsi14"]   = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

            tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
            df["atr14"]   = tr.ewm(com=13, adjust=False).mean()
            df["atr_pct"] = df["atr14"] / c

            df["avg_volume20"] = v.rolling(20).mean()
            df["volume_ratio"] = v / df["avg_volume20"].replace(0, np.nan)
            df["high20"] = h.rolling(20).max()
            df["low20"]  = lo.rolling(20).min()

            # Candle properties (for quality filters)
            df["body_pct"]      = (c - df["Open"]) / (h - lo + 1e-6)  # positive = bullish body
            df["close_pct_hi"]  = c / h                                 # 1.0 = close at daily high

            if reg_series is not None:
                df["regime"] = reg_series.reindex(df.index, method="ffill").fillna("NEUTRAL")
                df["rs20"]   = df["return20"] - r20.reindex(df.index, method="ffill")
                df["rs60"]   = df["return60"] - r60.reindex(df.index, method="ffill")
            else:
                df["regime"] = "NEUTRAL"
                df["rs20"] = df["rs60"] = np.nan

            stocks[t] = df
        except Exception:
            skipped += 1

    print(f"  Done: {len(stocks)} stocks, {skipped} skipped")
    return stocks


# ════════════════════════════════════════════════════════
# SIGNAL DEFINITIONS
# ════════════════════════════════════════════════════════

_NAN_CHECK = ["sma5","sma20","sma60","sma224","rsi14","atr_pct",
              "volume_ratio","return5","return20","high20","low20"]


def _ok(row) -> bool:
    return not any(pd.isna(row.get(c, np.nan)) for c in _NAN_CHECK)


# ── Base BREAKOUT (matches live signal_engine.py) ──
def _bo_base(row) -> bool:
    return (
        float(row.get("high20", 0)) > 0
        and float(row["Close"]) >= float(row["high20"]) * 0.995
        and float(row.get("volume_ratio", 0)) >= 1.5
    )

# ── Quality filters (independent tests) ──
def _f1_body(row) -> bool:
    """Strong bullish candle body (close > open, body >= 60% of range)."""
    return float(row.get("body_pct", 0)) >= 0.50

def _f2_close_near_hi(row) -> bool:
    """Close within 5% of daily high (limited upper wick)."""
    return float(row.get("close_pct_hi", 1)) >= 0.93

def _f3_strong_vol(row) -> bool:
    """Volume >= 2× average (meaningful breakout volume)."""
    return float(row.get("volume_ratio", 0)) >= 2.0

def _f4_sma224(row) -> bool:
    """SMA224 gate: close above long-term trend line."""
    sma224 = float(row.get("sma224", 0))
    return sma224 > 0 and float(row["Close"]) > sma224

def _f5_not_extended(row) -> bool:
    """Not over-extended: 20d return < 25%."""
    return float(row.get("return20", 0)) <= 0.25

def _f6_pos_rs(row) -> bool:
    """Positive relative strength vs KOSPI over 20 days."""
    rs20 = float(row.get("rs20", np.nan))
    return not pd.isna(rs20) and rs20 >= 0.0

def _f7_trend_context(row) -> bool:
    """Confirmed trend: sma20 slope positive, not in deep downtrend."""
    return (
        float(row.get("sma20_slope", 0)) > 0
        and float(row.get("return20", 0)) > -0.05
    )

# ── BREAKOUT signal variants ──
BREAKOUT_VARIANTS = {
    "BO_Base":              lambda r: _bo_base(r),
    "BO_F1_Body":           lambda r: _bo_base(r) and _f1_body(r),
    "BO_F2_CloseHi":        lambda r: _bo_base(r) and _f2_close_near_hi(r),
    "BO_F3_StrongVol":      lambda r: _bo_base(r) and _f3_strong_vol(r),
    "BO_F4_SMA224":         lambda r: _bo_base(r) and _f4_sma224(r),
    "BO_F5_NotExtended":    lambda r: _bo_base(r) and _f5_not_extended(r),
    "BO_F6_PosRS":          lambda r: _bo_base(r) and _f6_pos_rs(r),
    "BO_F7_Trend":          lambda r: _bo_base(r) and _f7_trend_context(r),
    "BO_CanqQuality":       lambda r: _bo_base(r) and _f1_body(r) and _f2_close_near_hi(r) and _f3_strong_vol(r),
    "BO_FullFilter":        lambda r: _bo_base(r) and _f1_body(r) and _f2_close_near_hi(r) and _f3_strong_vol(r) and _f4_sma224(r) and _f5_not_extended(r) and _f6_pos_rs(r),
}

# ── PULLBACK baseline ──
def sig_pullback_base(row) -> bool:
    """PULLBACK: confirmed uptrend + controlled pullback."""
    try:
        return (
            float(row.get("return20", 0)) > 0.01
            and float(row.get("sma20_slope", 0)) > 0
            and float(row.get("sma60_slope", 0)) > 0
            and float(row["Close"]) > float(row["sma60"])
            and -0.05 <= float(row.get("return5", 0)) <= -0.005
            and float(row["Close"]) > float(row["low20"]) * 1.01
            and 40 <= float(row.get("rsi14", 50)) <= 68
            and float(row.get("atr_pct", 0.05)) <= 0.12
        )
    except Exception: return False


# ════════════════════════════════════════════════════════
# EXIT POLICIES
# ════════════════════════════════════════════════════════

EXIT_POLICIES = {
    # Standard (from Phase 1.5 best for PULLBACK)
    "B":            (-0.04, +0.08, +0.05, 0.025),
    # Wider (better for BREAKOUT: let winners run)
    "D":            (-0.05, +0.10, +0.05, 0.030),
    # BREAKOUT-specific: wide trailing, no hard take
    "BO_Wide":      (-0.05, +0.99, +0.04, 0.030),   # take=99% (effectively no fixed take)
    # Aggressive trailing
    "BO_Aggr":      (-0.06, +0.99, +0.06, 0.040),
}
MAX_HOLD_DAYS = 10   # breakouts may need more time


def _exit_sim(entry_price: float, future_rows: pd.DataFrame,
              stop: float, take: float, ts: float, tg: float,
              max_hold: int) -> tuple[float, int, str]:
    """Simulate a single trade exit. Returns (net_pnl, hold_days, exit_reason)."""
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

        if op <= stop_p:
            return (op / entry_price - 1) - ROUND_TRIP_COST, i + 1, "stop_gap"
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


# ════════════════════════════════════════════════════════
# TRADE GENERATOR
# ════════════════════════════════════════════════════════

def generate_trades(ticker: str, df: pd.DataFrame, sig_fn, policy: str, max_hold: int) -> list[dict]:
    bt = df[df.index >= BACKTEST_START].copy()
    if len(bt) < 5: return []

    stop, take, trs, trg = EXIT_POLICIES[policy]
    trades = []
    pos_end = -1

    for i in range(len(bt) - 1):
        if i <= pos_end: continue
        row = bt.iloc[i]
        if not _ok(row): continue
        if not sig_fn(row): continue

        nxt = bt.iloc[i + 1]
        ep = float(nxt["Open"])
        if pd.isna(ep) or ep <= 0: continue

        future_end = min(i + 1 + max_hold + 1, len(bt))
        future = bt.iloc[i + 1: future_end]
        net_pnl, hold_d, reason = _exit_sim(ep, future, stop, take, trs, trg, max_hold)

        exit_idx = (i + 1) + hold_d - 1
        pos_end  = exit_idx
        exit_date = bt.index[min(exit_idx, len(bt) - 1)]

        trades.append({
            "ticker":      ticker,
            "signal_date": bt.index[i],
            "entry_date":  bt.index[i + 1],
            "exit_date":   exit_date,
            "entry_price": ep,
            "net_pnl":     net_pnl,
            "hold_days":   hold_d,
            "exit_reason": reason,
            "regime":      str(row.get("regime", "NEUTRAL")),
            "year":        bt.index[i].year,
            "score_proxy": float(row.get("volume_ratio", 1.0)),
        })
    return trades


# ════════════════════════════════════════════════════════
# PORTFOLIO SIMULATION
# ════════════════════════════════════════════════════════

def portfolio_simulate(all_trades: list[dict],
                       initial_capital: float = INITIAL_CAPITAL,
                       max_positions: int = MAX_POSITIONS) -> list[dict]:
    """Returns portfolio-executed trades (capital-constrained)."""
    cands = sorted(all_trades, key=lambda t: (t["entry_date"], -t["score_proxy"]))
    cash = float(initial_capital)
    open_pos: list[dict] = []
    executed: list[dict] = []

    def _expire(before):
        nonlocal cash
        still = []
        for p in open_pos:
            if p["exit_date"] <= before:
                cash += p["allocated"] * (1 + p["net_pnl"])
                executed.append(p)
            else:
                still.append(p)
        open_pos[:] = still

    for cand in cands:
        _expire(cand["entry_date"])
        if len(open_pos) >= max_positions: continue
        alloc = min(cash / max_positions, cash)
        if alloc < MIN_ALLOCATION: continue
        cash -= alloc
        open_pos.append({**cand, "allocated": alloc})

    _expire(datetime(2099, 1, 1))
    return executed


# ════════════════════════════════════════════════════════
# METRICS
# ════════════════════════════════════════════════════════

def metrics_portfolio(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "pf": 0, "wr": 0, "avg": 0, "stop_r": 0, "trail_r": 0, "take_r": 0}
    df  = pd.DataFrame(trades)
    pnl = df["net_pnl"]
    w   = pnl[pnl > 0]
    l   = pnl[pnl <= 0]
    # Portfolio-weight by allocated capital
    w_alloc = (df.loc[pnl > 0, "net_pnl"] * df.loc[pnl > 0, "allocated"]).sum()
    l_alloc = abs((df.loc[pnl <= 0, "net_pnl"] * df.loc[pnl <= 0, "allocated"]).sum())
    pf = w_alloc / l_alloc if l_alloc > 0 else float("inf")
    return {
        "n":      len(df),
        "pf":     pf,
        "wr":     len(w) / len(df),
        "avg":    pnl.mean(),
        "avg_w":  w.mean() if len(w) else 0,
        "avg_l":  l.mean() if len(l) else 0,
        "stop_r": df["exit_reason"].str.startswith("stop").mean(),
        "trail_r":  (df["exit_reason"] == "trail").mean(),
        "take_r":  (df["exit_reason"] == "take").mean(),
        "time_r":  (df["exit_reason"] == "time").mean(),
        "avg_hold": df["hold_days"].mean(),
    }


def metrics_raw(trades: list[dict]) -> dict:
    """Independent-trade metrics (unconstrained, for signal quality)."""
    if not trades:
        return {"n": 0, "pf": 0, "wr": 0, "avg": 0}
    df  = pd.DataFrame(trades)
    pnl = df["net_pnl"]
    w = pnl[pnl > 0]; l = pnl[pnl <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) > 0 and l.sum() != 0 else float("inf")
    return {
        "n":     len(df),
        "pf":    pf,
        "wr":    len(w) / len(df),
        "avg":   pnl.mean(),
        "stop_r": df["exit_reason"].str.startswith("stop").mean(),
        "trail_r": (df["exit_reason"] == "trail").mean(),
        "take_r": (df["exit_reason"] == "take").mean(),
    }


# ════════════════════════════════════════════════════════
# FORMATTING
# ════════════════════════════════════════════════════════

def fp(v, d=2): return f"{v*100:{'+' if v>=0 else ''}.{d}f}%"
def pft(pf): return " <<PF>1.10" if pf >= 1.10 else (" <PF>=1.00" if pf >= 1.00 else "")

def _hdr_raw():
    return f"{'Signal':<24} {'N':>5} {'PF':>6} {'WR%':>6} {'AvgNet':>8} {'Stop%':>7} {'Trail%':>7} {'Take%':>7}"

def _row_raw(label, m):
    pf = m['pf']
    return (f"{label:<24} {m['n']:>5} {pf:>6.2f}{pft(pf)} "
            f"{m['wr']*100:>5.1f}% {fp(m['avg']):>8} "
            f"{m['stop_r']*100:>6.1f}% {m['trail_r']*100:>6.1f}% {m['take_r']*100:>6.1f}%")

def _hdr_port():
    return f"{'Signal/Policy':<26} {'N':>5} {'PF(port)':>10} {'WR%':>6} {'AvgNet':>8} {'Stop%':>7} {'Trail%':>7} {'Hold':>5}"

def _row_port(label, m):
    pf = m['pf']
    return (f"{label:<26} {m['n']:>5} {pf:>10.2f}{pft(pf)} "
            f"{m['wr']*100:>5.1f}% {fp(m['avg']):>8} "
            f"{m['stop_r']*100:>6.1f}% {m['trail_r']*100:>6.1f}% {m.get('avg_hold',0):>5.1f}d")


# ════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════

def main():
    t0 = time.time()
    print("=" * 70)
    print("OPTION C — BREAKOUT STRATEGY ISOLATED")
    print(f"Period  : {BACKTEST_START} → {TODAY}")
    print(f"Universe: KOSPI top {KOSPI_TOP_N} + KOSDAQ top {KOSDAQ_TOP_N}")
    print("=" * 70)
    print("[SURVIVORSHIP BIAS] Current (2026) market-cap universe applied to 2018-2026 data.\n")

    print("[1] Loading universe...")
    tickers = list(dict.fromkeys(
        _top_tickers("KOSPI", KOSPI_TOP_N) + _top_tickers("KOSDAQ", KOSDAQ_TOP_N)
    ))
    print(f"  {len(tickers)} tickers")

    print("[2] Downloading data...")
    stocks = load_data(tickers)
    print(f"  {len(stocks)} stocks loaded in {time.time()-t0:.0f}s\n")

    # ══════════════════════════════════════════
    # Part 1: Raw signal quality — all 10 breakout variants + pullback baseline
    # (unconstrained, for signal quality assessment)
    # ══════════════════════════════════════════
    print("=" * 70)
    print("PART 1: BREAKOUT SIGNAL QUALITY  (Policy B, 10d, independent trades)")
    print("        No capital constraints — signal quality comparison only")
    print("=" * 70)
    print(_hdr_raw())
    print("-" * 80)

    bo_trades_store = {}
    for variant_name, sig_fn in BREAKOUT_VARIANTS.items():
        all_trades = []
        for ticker, df in stocks.items():
            all_trades.extend(generate_trades(ticker, df, sig_fn, "B", MAX_HOLD_DAYS))
        m = metrics_raw(all_trades)
        bo_trades_store[variant_name] = all_trades
        print(_row_raw(variant_name, m))

    # PULLBACK baseline
    pb_trades = []
    for ticker, df in stocks.items():
        pb_trades.extend(generate_trades(ticker, df, sig_pullback_base, "B", MAX_HOLD_DAYS))
    m_pb = metrics_raw(pb_trades)
    print("-" * 80)
    print(_row_raw("PULLBACK_Base", m_pb))

    # ══════════════════════════════════════════
    # Part 2: BREAKOUT regime split
    # ══════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PART 2: BREAKOUT BY REGIME  (BO_Base and BO_FullFilter, Policy B, 10d)")
    print("=" * 70)

    for variant in ["BO_Base", "BO_FullFilter"]:
        trades = bo_trades_store[variant]
        df_all = pd.DataFrame(trades) if trades else pd.DataFrame()
        if df_all.empty:
            print(f"  {variant}: no trades")
            continue

        print(f"\n  ── {variant}  (total N={len(trades)}) ──")
        print(f"  {'Regime':<10} {'N':>5} {'PF':>6} {'WR%':>6} {'AvgNet':>8} {'Stop%':>7}")
        print("  " + "-" * 48)
        for reg in ["BULL","NEUTRAL","WEAK","CRASH"]:
            sub = df_all[df_all["regime"] == reg]
            if len(sub) == 0: continue
            m = metrics_raw(sub.to_dict("records"))
            print(f"  {reg:<10} {m['n']:>5} {m['pf']:>6.2f}{pft(m['pf'])} "
                  f"{m['wr']*100:>5.1f}% {fp(m['avg']):>8} {m['stop_r']*100:>6.1f}%")

        print(f"\n  {'Year':<6} {'N':>5} {'PF':>6} {'WR%':>6} {'AvgNet':>8}")
        print("  " + "-" * 35)
        for yr in sorted(df_all["year"].unique()):
            sub = df_all[df_all["year"] == yr]
            m = metrics_raw(sub.to_dict("records"))
            print(f"  {yr:<6} {m['n']:>5} {m['pf']:>6.2f}{pft(m['pf'])} {m['wr']*100:>5.1f}% {fp(m['avg']):>8}")

    # PULLBACK same breakdown
    if pb_trades:
        df_pb = pd.DataFrame(pb_trades)
        print(f"\n  ── PULLBACK_Base  (total N={len(pb_trades)}) ──")
        print(f"  {'Regime':<10} {'N':>5} {'PF':>6} {'WR%':>6} {'AvgNet':>8}")
        print("  " + "-" * 40)
        for reg in ["BULL","NEUTRAL","WEAK","CRASH"]:
            sub = df_pb[df_pb["regime"] == reg]
            if len(sub) == 0: continue
            m = metrics_raw(sub.to_dict("records"))
            print(f"  {reg:<10} {m['n']:>5} {m['pf']:>6.2f}{pft(m['pf'])} {m['wr']*100:>5.1f}% {fp(m['avg']):>8}")

    # ══════════════════════════════════════════
    # Part 3: Exit policy comparison for best BREAKOUT variant
    # ══════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PART 3: BREAKOUT EXIT POLICY  (BO_Base, all regimes, raw/independent)")
    print("=" * 70)
    print(_hdr_raw())
    print("-" * 80)
    sig_fn_base = BREAKOUT_VARIANTS["BO_Base"]
    for pol_name, (stop, take, trs, trg) in EXIT_POLICIES.items():
        trades = []
        for ticker, df in stocks.items():
            trades.extend(generate_trades(ticker, df, sig_fn_base, pol_name, MAX_HOLD_DAYS))
        m = metrics_raw(trades)
        print(_row_raw(f"BO_Base|{pol_name}", m))

    print("\n--- Same comparison for BO_FullFilter ---")
    sig_fn_full = BREAKOUT_VARIANTS["BO_FullFilter"]
    for pol_name in EXIT_POLICIES:
        trades = []
        for ticker, df in stocks.items():
            trades.extend(generate_trades(ticker, df, sig_fn_full, pol_name, MAX_HOLD_DAYS))
        m = metrics_raw(trades)
        print(_row_raw(f"BO_Full|{pol_name}", m))

    # ══════════════════════════════════════════
    # Part 4: Portfolio-level simulation (capital-constrained)
    # ══════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PART 4: PORTFOLIO SIMULATION  (₩5M, max 3 positions, capital-constrained)")
    print("=" * 70)
    print(_hdr_port())
    print("-" * 90)

    for variant in ["BO_Base", "BO_CanqQuality", "BO_FullFilter"]:
        sig_fn = BREAKOUT_VARIANTS[variant]
        for pol_name in ["B", "D", "BO_Wide", "BO_Aggr"]:
            trades = []
            for ticker, df in stocks.items():
                trades.extend(generate_trades(ticker, df, sig_fn, pol_name, MAX_HOLD_DAYS))
            exec_t = portfolio_simulate(trades)
            m = metrics_portfolio(exec_t)
            print(_row_port(f"{variant}|{pol_name}", m))

    # PULLBACK portfolio for comparison
    print("-" * 90)
    for pol_name in ["B", "D"]:
        trades = []
        for ticker, df in stocks.items():
            trades.extend(generate_trades(ticker, df, sig_pullback_base, pol_name, MAX_HOLD_DAYS))
        exec_t = portfolio_simulate(trades)
        m = metrics_portfolio(exec_t)
        print(_row_port(f"PULLBACK_Base|{pol_name}", m))

    # ══════════════════════════════════════════
    # Part 5: BULL-only regime gate (best BREAKOUT variants)
    # ══════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PART 5: BULL+NEUTRAL ONLY — BREAKOUT (portfolio-constrained)")
    print("=" * 70)
    print(_hdr_port())
    print("-" * 90)

    for variant in ["BO_Base", "BO_CanqQuality", "BO_FullFilter"]:
        sig_fn = BREAKOUT_VARIANTS[variant]
        for pol_name in ["B", "BO_Wide"]:
            # generate with regime filter
            trades = []
            for ticker, df in stocks.items():
                for t in generate_trades(ticker, df, sig_fn, pol_name, MAX_HOLD_DAYS):
                    if t["regime"] in {"BULL", "NEUTRAL"}:
                        trades.append(t)
            exec_t = portfolio_simulate(trades)
            m = metrics_portfolio(exec_t)
            print(_row_port(f"{variant}|{pol_name}|BN", m))

    # BULL+NEUTRAL+CRASH (exceptional breakouts in crash)
    print("\n  --- CRASH breakouts included (per Codex: exceptional in any regime) ---")
    for variant in ["BO_FullFilter"]:
        sig_fn = BREAKOUT_VARIANTS[variant]
        for pol_name in ["BO_Wide"]:
            trades = []
            for ticker, df in stocks.items():
                for t in generate_trades(ticker, df, sig_fn, pol_name, MAX_HOLD_DAYS):
                    if t["regime"] in {"BULL", "NEUTRAL", "CRASH"}:
                        trades.append(t)
            exec_t = portfolio_simulate(trades)
            m = metrics_portfolio(exec_t)
            print(_row_port(f"{variant}|{pol_name}|BNC", m))

    elapsed = time.time() - t0
    print(f"\n{'='*70}")
    print(f"OPTION C COMPLETE — Runtime: {elapsed:.0f}s")
    print(f"{'='*70}")
    print("\nAudit notes:")
    print("  - Entry: next-day open (T2/T3 confirmations excluded per Phase 1.5 finding)")
    print("  - Gap-through stop: open <= stop_price → exit at open")
    print("  - Same-day stop wins over take (conservative)")
    print("  - BO_Wide policy: no fixed take profit (trail-only exit)")
    print("  - Survivorship bias: current (2026) universe composition applied to 2018-2026")


if __name__ == "__main__":
    main()
