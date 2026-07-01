"""
backtest_phase1_5.py — KR Bot Phase 1.5 Extended Backtest
==========================================================
Goal: Find combination achieving PF > 1.10 after 0.35% round-trip costs.

Tests:
  A. ATR distribution audit (verify -2% stop was too tight)
  B. Entry strategy sweep: RS fixed thresholds vs percentile ranking
  C. Market regime split (BULL/NEUTRAL/WEAK/CRASH separately)
  D. Regime-gated entries (restrict to BULL+NEUTRAL only)
  E. Entry timing (next-day open vs confirmation signals)
  F. Exit policy comparison on best-found entry

Run: python3 backtest_phase1_5.py
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")

import sys
import time
import numpy as np
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

TODAY          = datetime(2026, 6, 4)
DATA_START     = datetime(2023, 1, 1)
BACKTEST_START = TODAY - timedelta(days=365 * 2 + 10)
TODAY_STR          = TODAY.strftime("%Y-%m-%d")
DATA_START_STR     = DATA_START.strftime("%Y-%m-%d")
BACKTEST_START_STR = BACKTEST_START.strftime("%Y-%m-%d")

KOSPI_TOP_N  = 150
KOSDAQ_TOP_N = 75
MIN_ROWS     = 280
ROUND_TRIP   = 0.0035


# ─────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, kospi_regime: pd.Series | None) -> pd.DataFrame:
    df = df.copy()
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

    delta    = c.diff()
    avg_gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    avg_loss = (-delta).clip(lower=0).ewm(com=13, adjust=False).mean()
    df["rsi14"] = 100 - (100 / (1 + avg_gain / avg_loss.replace(0, np.nan)))

    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    df["atr14"]   = tr.ewm(com=13, adjust=False).mean()
    df["atr_pct"] = df["atr14"] / c

    df["avg_volume20"]        = v.rolling(20).mean()
    df["volume_ratio"]        = v / df["avg_volume20"].replace(0, np.nan)
    df["trading_value_avg20"] = (c * v).rolling(20).mean()

    df["high20"] = h.rolling(20).max()
    df["low20"]  = lo.rolling(20).min()

    if kospi_regime is not None:
        aligned = kospi_regime.reindex(df.index, method="ffill")
        df["regime"] = aligned.values
    else:
        df["regime"] = "NEUTRAL"

    return df


# ─────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────

def _get_top_tickers(market: str, n: int) -> list[str]:
    try:
        listing = fdr.StockListing(market)
        for mc in ["Marcap", "MarCap", "시가총액", "MktCap"]:
            if mc in listing.columns:
                listing = listing.sort_values(mc, ascending=False)
                break
        for cc in ["Code", "Symbol", "종목코드"]:
            if cc in listing.columns:
                return listing[cc].head(n).tolist()
    except Exception as e:
        print(f"  Warning: {market} listing failed: {e}")
    return []


def build_kospi_regime(kospi_idx: pd.DataFrame) -> pd.Series:
    r20 = kospi_idx["Close"].pct_change(20)
    r60 = kospi_idx["Close"].pct_change(60)

    def _classify(i):
        v20 = r20.iloc[i] if i < len(r20) else np.nan
        v60 = r60.iloc[i] if i < len(r60) else np.nan
        if pd.isna(v20):
            return "NEUTRAL"
        if v20 <= -0.08:
            return "CRASH"
        if v20 < -0.03:
            return "WEAK"
        if v20 > 0.03 and (pd.isna(v60) or v60 > 0.02):
            return "BULL"
        return "NEUTRAL"

    return pd.Series([_classify(i) for i in range(len(kospi_idx))], index=kospi_idx.index)


def load_all_data(tickers: list[str]):
    print("  Downloading KS11 (KOSPI index)...")
    try:
        kospi_idx = fdr.DataReader("KS11", DATA_START_STR, TODAY_STR)
        kospi_idx.index = pd.to_datetime(kospi_idx.index)
        regime_series = build_kospi_regime(kospi_idx)
        print(f"  KS11: {len(kospi_idx)} rows")
        rc = regime_series.value_counts()
        print(f"  Regime dist: {rc.to_dict()}")
    except Exception as e:
        print(f"  Warning: KS11 failed ({e})")
        kospi_idx = None
        regime_series = None

    # Also keep kospi returns for RS computation
    kospi_ret20 = kospi_idx["Close"].pct_change(20) if kospi_idx is not None else None
    kospi_ret60 = kospi_idx["Close"].pct_change(60) if kospi_idx is not None else None

    stocks: dict[str, pd.DataFrame] = {}
    skipped = 0
    n = len(tickers)

    for i, t in enumerate(tickers):
        if (i + 1) % 50 == 0 or i == 0:
            print(f"  [{i+1}/{n}] loaded {len(stocks)} stocks...")
        try:
            df = fdr.DataReader(t, DATA_START_STR, TODAY_STR)
            df.index = pd.to_datetime(df.index)
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                if col not in df.columns:
                    raise ValueError(f"missing {col}")
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            if len(df) < MIN_ROWS:
                skipped += 1
                continue

            # Add RS vs KOSPI
            if kospi_ret20 is not None:
                aligned_r20 = kospi_ret20.reindex(df.index, method="ffill")
                aligned_r60 = kospi_ret60.reindex(df.index, method="ffill")
                df = compute_indicators(df, regime_series)
                df["kospi_return20"] = aligned_r20.values
                df["kospi_return60"] = aligned_r60.values
                df["rs20"] = df["return20"] - df["kospi_return20"]
                df["rs60"] = df["return60"] - df["kospi_return60"]
            else:
                df = compute_indicators(df, None)
                df["kospi_return20"] = np.nan
                df["kospi_return60"] = np.nan
                df["rs20"] = np.nan
                df["rs60"] = np.nan

            stocks[t] = df
        except Exception:
            skipped += 1

    print(f"  Done: {len(stocks)} stocks loaded, {skipped} skipped")
    return stocks


# ─────────────────────────────────────────────────────────
# RS PERCENTILE (cross-sectional daily rank)
# ─────────────────────────────────────────────────────────

def compute_rs_percentiles(stocks: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    rs20_pivot = pd.DataFrame({t: df["rs20"] for t, df in stocks.items()})
    rs60_pivot = pd.DataFrame({t: df["rs60"] for t, df in stocks.items()})
    rs20_pct   = rs20_pivot.rank(axis=1, pct=True)
    rs60_pct   = rs60_pivot.rank(axis=1, pct=True)
    combined   = 0.6 * rs20_pct + 0.4 * rs60_pct
    return {t: combined[t] for t in combined.columns}


# ─────────────────────────────────────────────────────────
# ENTRY SIGNAL FUNCTIONS
# ─────────────────────────────────────────────────────────

def _trend_confirmed(row) -> bool:
    try:
        return (
            row["return20"]    > 0.02
            and row["return60"]  > 0.03
            and row["sma20_slope"] > 0
            and row["sma60_slope"] > 0
            and row["Close"]     > row["sma60"]
            and row["rsi14"]     >= 40
            and row["atr_pct"]   <= 0.12
        )
    except Exception:
        return False


def _pullback_valid(row) -> bool:
    try:
        return (
            -0.05 <= row["return5"] <= -0.005
            and row["Close"] > row["low20"] * 1.01
            and row["volume_ratio"] <= 1.8
        )
    except Exception:
        return False


def s1_defense_long(row, rs_pct=None) -> bool:
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


def s2_sma224(row, rs_pct=None) -> bool:
    try:
        return row["sma224"] > 0 and row["Close"] > row["sma224"] and s1_defense_long(row)
    except Exception:
        return False


def s3_rs_fixed_0p5(row, rs_pct=None) -> bool:
    try:
        return _trend_confirmed(row) and _pullback_valid(row) and row["rs20"] >= 0.005
    except Exception:
        return False


def s4_rs_fixed_0p0(row, rs_pct=None) -> bool:
    try:
        return _trend_confirmed(row) and _pullback_valid(row) and row["rs20"] >= 0.0
    except Exception:
        return False


def s5_rs_fixed_neg2(row, rs_pct=None) -> bool:
    try:
        return _trend_confirmed(row) and _pullback_valid(row) and row["rs20"] >= -0.02
    except Exception:
        return False


def s6_rs_top50pct(row, rs_pct=None) -> bool:
    try:
        if rs_pct is None or pd.isna(rs_pct):
            return False
        return _trend_confirmed(row) and _pullback_valid(row) and rs_pct >= 0.50
    except Exception:
        return False


def s7_rs_top30pct(row, rs_pct=None) -> bool:
    try:
        if rs_pct is None or pd.isna(rs_pct):
            return False
        return _trend_confirmed(row) and _pullback_valid(row) and rs_pct >= 0.70
    except Exception:
        return False


def s8_rs_top30pct_volcontract(row, rs_pct=None) -> bool:
    try:
        if rs_pct is None or pd.isna(rs_pct):
            return False
        return (
            _trend_confirmed(row)
            and _pullback_valid(row)
            and rs_pct >= 0.70
            and 0.25 <= row["volume_ratio"] <= 0.85
        )
    except Exception:
        return False


SIGNALS = {
    "S1_DefLong":     s1_defense_long,
    "S2_SMA224":      s2_sma224,
    "S3_RS>=0.5%":    s3_rs_fixed_0p5,
    "S4_RS>=0.0%":    s4_rs_fixed_0p0,
    "S5_RS>=-2%":     s5_rs_fixed_neg2,
    "S6_RSTop50%":    s6_rs_top50pct,
    "S7_RSTop30%":    s7_rs_top30pct,
    "S8_RSTop30%+Vol":s8_rs_top30pct_volcontract,
}

NEEDS_RS_PCT = {"S6_RSTop50%", "S7_RSTop30%", "S8_RSTop30%+Vol"}


# ─────────────────────────────────────────────────────────
# EXIT SIMULATION
# ─────────────────────────────────────────────────────────

def _exit_params(policy: str, atr: float | None):
    a = atr if atr and not np.isnan(atr) else 0.04
    if policy == "B":
        return -0.04, 0.08, 0.05, 0.025
    if policy == "D":
        return -0.05, 0.10, 0.05, 0.03
    if policy == "C":
        stop = -max(0.025, min(0.06, 1.5 * a))
        take = abs(stop) * 2.2
        return stop, take, abs(stop), a * 0.8
    raise ValueError(f"Unknown policy {policy}")


def _simulate(entry: float, future: pd.DataFrame, stop: float, take: float,
              trail_start: float, trail_gap: float, max_hold: int):
    peak = entry
    trailing = False
    for i, (_, bar) in enumerate(future.iterrows()):
        if i >= max_hold:
            ep = float(bar["Close"])
            return (ep / entry - 1) - ROUND_TRIP, i + 1, "time"
        lo, hi = float(bar["Low"]), float(bar["High"])
        if (lo - entry) / entry <= stop:
            return stop - ROUND_TRIP, i + 1, "stop"
        if (hi - entry) / entry >= take:
            return take - ROUND_TRIP, i + 1, "take"
        peak = max(peak, hi)
        if (peak - entry) / entry >= trail_start:
            trailing = True
        if trailing and (peak - lo) / peak >= trail_gap:
            ep = peak * (1 - trail_gap)
            return (ep / entry - 1) - ROUND_TRIP, i + 1, "trail"
    if len(future) == 0:
        return -ROUND_TRIP, 0, "time"
    ep = float(future.iloc[-1]["Close"])
    return (ep / entry - 1) - ROUND_TRIP, len(future), "time"


# ─────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────────────────

NAN_COLS = [
    "sma5", "sma20", "sma60", "sma224", "rsi14", "atr_pct",
    "volume_ratio", "return5", "return20", "return60",
    "sma20_slope", "sma60_slope", "low20", "rs20",
]


def run_backtest(
    stocks: dict[str, pd.DataFrame],
    strategy_name: str,
    exit_policy: str,
    max_hold: int = 5,
    regime_filter: set | None = None,
    entry_timing: str = "T1",
    rs_pct_data: dict | None = None,
) -> list[dict]:
    """
    entry_timing:
      T1 = next-day open (always)
      T2 = next-day close if close > signal-day SMA5
      T3 = next-day close if close > signal-day high (acceleration confirmation)
    """
    sig_fn = SIGNALS[strategy_name]
    trades = []

    for ticker, df in stocks.items():
        bt = df[df.index >= BACKTEST_START_STR].copy()
        if len(bt) < 5:
            continue

        rs_series = rs_pct_data.get(ticker) if rs_pct_data else None
        pos_end = -1

        for i in range(len(bt) - 1):
            if i <= pos_end:
                continue

            row = bt.iloc[i]

            if any(pd.isna(row.get(c, np.nan)) for c in NAN_COLS):
                continue

            if regime_filter and row.get("regime", "NEUTRAL") not in regime_filter:
                continue

            rs_val = None
            if rs_series is not None:
                try:
                    rs_val = rs_series.loc[bt.index[i]]
                    if pd.isna(rs_val):
                        rs_val = None
                except Exception:
                    rs_val = None

            if not sig_fn(row, rs_val):
                continue

            nxt = bt.iloc[i + 1]

            if entry_timing == "T1":
                ep = float(nxt["Open"])
                ei = i + 1
            elif entry_timing == "T2":
                sma5_thresh = float(row.get("sma5", row["Close"]))
                if float(nxt["Close"]) <= sma5_thresh:
                    continue
                ep = float(nxt["Close"])
                ei = i + 1
            elif entry_timing == "T3":
                prev_high = float(row["High"])
                if float(nxt["Close"]) <= prev_high:
                    continue
                ep = float(nxt["Close"])
                ei = i + 1
            else:
                ep = float(nxt["Open"])
                ei = i + 1

            if pd.isna(ep) or ep <= 0:
                continue

            atr_v = float(row["atr_pct"]) if not pd.isna(row.get("atr_pct", np.nan)) else None
            stop, take, ts, tg = _exit_params(exit_policy, atr_v)
            future_end = min(ei + max_hold + 1, len(bt))
            future = bt.iloc[ei:future_end]

            net, hold_d, reason = _simulate(ep, future, stop, take, ts, tg, max_hold)
            pos_end = ei + hold_d - 1

            trades.append({
                "ticker":      ticker,
                "entry_date":  bt.index[i],
                "net_pnl":     net,
                "hold_days":   hold_d,
                "exit_reason": reason,
                "regime":      str(row.get("regime", "NEUTRAL")),
                "year":        bt.index[i].year,
            })

    return trades


# ─────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────

def metrics(trades) -> dict:
    if not trades:
        return dict(n=0, wr=0, avg=0, pf=0, stop_r=0, take_r=0, trail_r=0,
                    time_r=0, avg_w=0, avg_l=0, avg_hold=0, cum=0)
    df  = pd.DataFrame(trades)
    pnl = df["net_pnl"]
    w   = pnl[pnl > 0]
    l   = pnl[pnl <= 0]
    pf  = w.sum() / abs(l.sum()) if len(l) > 0 and l.sum() != 0 else float("inf")
    return dict(
        n       = len(df),
        wr      = len(w) / len(df),
        avg     = pnl.mean(),
        pf      = pf,
        stop_r  = (df["exit_reason"] == "stop").mean(),
        take_r  = (df["exit_reason"] == "take").mean(),
        trail_r = (df["exit_reason"] == "trail").mean(),
        time_r  = (df["exit_reason"] == "time").mean(),
        avg_w   = w.mean() if len(w) else 0,
        avg_l   = l.mean() if len(l) else 0,
        avg_hold= df["hold_days"].mean(),
        cum     = pnl.sum(),
    )


# ─────────────────────────────────────────────────────────
# FORMATTING
# ─────────────────────────────────────────────────────────

def pf_tag(pf):
    if pf >= 1.10:
        return " <<< PF>1.10"
    if pf >= 1.00:
        return " < PF>=1.00"
    return ""


def fp(v, d=2):
    return f"{v * 100:{'+' if v >= 0 else ''}.{d}f}%"


def _hdr():
    return (f"{'Label':<26} {'N':>5} {'WR%':>6} {'AvgNet':>8} "
            f"{'PF':>6} {'Stop%':>6} {'Take%':>6} {'Trail%':>7} {'Time%':>6} "
            f"{'AvgW':>7} {'AvgL':>7}")


def _row(label, m):
    return (
        f"{label:<26} {m['n']:>5} {m['wr']*100:>5.1f}% {fp(m['avg']):>8} "
        f"{m['pf']:>6.2f} {m['stop_r']*100:>5.1f}% {m['take_r']*100:>5.1f}% "
        f"{m['trail_r']*100:>6.1f}% {m['time_r']*100:>5.1f}% "
        f"{fp(m['avg_w']):>7} {fp(m['avg_l']):>7}"
        f"{pf_tag(m['pf'])}"
    )


def _sep():
    return "-" * 110


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    print("=" * 70)
    print("KR BOT PHASE 1.5 BACKTEST — Target: PF > 1.10 after costs")
    print(f"Window : {BACKTEST_START_STR} → {TODAY_STR}")
    print(f"Universe: KOSPI top {KOSPI_TOP_N} + KOSDAQ top {KOSDAQ_TOP_N}")
    print("=" * 70)

    # ── Universe & data ──
    print("\n[1] Loading universe...")
    tickers = _get_top_tickers("KOSPI", KOSPI_TOP_N) + _get_top_tickers("KOSDAQ", KOSDAQ_TOP_N)
    tickers = list(dict.fromkeys(tickers))
    print(f"  {len(tickers)} tickers")

    print("[2] Downloading price data...")
    stocks = load_all_data(tickers)

    # ── RS percentile ──
    print("[3] Computing cross-sectional RS percentiles...")
    rs_pct = compute_rs_percentiles(stocks)

    elapsed_load = time.time() - t0
    print(f"  Data ready in {elapsed_load:.0f}s — {len(stocks)} stocks\n")

    # ════════════════════════════════════════════════════
    # A. ATR DISTRIBUTION AUDIT
    # ════════════════════════════════════════════════════
    print("=" * 70)
    print("A. ATR DISTRIBUTION AUDIT")
    print("=" * 70)
    atr_rows = []
    kospi_set = set(_get_top_tickers("KOSPI", KOSPI_TOP_N))
    for t, df in stocks.items():
        bt = df[df.index >= BACKTEST_START_STR]["atr_pct"].dropna()
        mkt = "KOSPI" if t in kospi_set else "KOSDAQ"
        for v in bt:
            if 0 < v < 0.30:
                atr_rows.append({"market": mkt, "atr_pct": v})
    adf = pd.DataFrame(atr_rows)

    print(f"  Observations : {len(adf):,}")
    print(f"  Overall median ATR%: {adf['atr_pct'].median()*100:.2f}%")
    print(f"  P25 / P75 ATR%     : {adf['atr_pct'].quantile(0.25)*100:.2f}% / {adf['atr_pct'].quantile(0.75)*100:.2f}%")
    print(f"  Mean ATR%          : {adf['atr_pct'].mean()*100:.2f}%")
    print(f"  P90 ATR%           : {adf['atr_pct'].quantile(0.90)*100:.2f}%")
    for mkt in ["KOSPI", "KOSDAQ"]:
        sub = adf[adf["market"] == mkt]["atr_pct"]
        if len(sub):
            print(f"  {mkt} median={sub.median()*100:.2f}%  P25={sub.quantile(0.25)*100:.2f}%  P75={sub.quantile(0.75)*100:.2f}%")
    print(f"\n  Stop -2% tighter than daily ATR in {(adf['atr_pct'] > 0.02).mean()*100:.0f}% of rows")
    print(f"  Stop -4% tighter than daily ATR in {(adf['atr_pct'] > 0.04).mean()*100:.0f}% of rows")

    # ════════════════════════════════════════════════════
    # B. ENTRY STRATEGY COMPARISON (Policy B, 5d, all regimes, T1)
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("B. ENTRY STRATEGY COMPARISON  (Policy B, 5d hold, all regimes)")
    print("=" * 70)
    print(_hdr())
    print(_sep())

    all_results: dict[str, dict] = {}
    for sname, sfn in SIGNALS.items():
        need_rs = sname in NEEDS_RS_PCT
        tr = run_backtest(stocks, sname, "B", max_hold=5,
                          regime_filter=None, entry_timing="T1",
                          rs_pct_data=rs_pct if need_rs else None)
        m = metrics(tr)
        all_results[sname] = {"trades": tr, "m": m}
        print(_row(sname, m))

    # Pick top-3 by PF for detailed analysis
    ranked = sorted(all_results.items(), key=lambda x: x[1]["m"]["pf"], reverse=True)
    top3   = [r[0] for r in ranked[:3]]

    # ════════════════════════════════════════════════════
    # C. REGIME SPLIT for top-3 strategies
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("C. REGIME + YEAR SPLIT  (Policy B, 5d, top-3 strategies)")
    print("=" * 70)

    for sname in top3:
        trades_df = pd.DataFrame(all_results[sname]["trades"])
        m_all     = all_results[sname]["m"]
        print(f"\n  ── {sname}  (overall: N={m_all['n']}, PF={m_all['pf']:.2f}) ──")

        if trades_df.empty:
            print("    (no trades)")
            continue

        print(f"  {'Regime':<12} {'N':>5} {'WR%':>6} {'AvgNet':>8} {'PF':>6} {'Stop%':>6} {'Cum':>9}")
        print("  " + "-" * 55)
        for regime in ["BULL", "NEUTRAL", "WEAK", "CRASH"]:
            sub = trades_df[trades_df["regime"] == regime]
            if len(sub) == 0:
                continue
            m = metrics(sub.to_dict("records"))
            print(f"  {regime:<12} {m['n']:>5} {m['wr']*100:>5.1f}% {fp(m['avg']):>8} "
                  f"{m['pf']:>6.2f} {m['stop_r']*100:>5.1f}% {fp(m['cum']):>9}{pf_tag(m['pf'])}")

        print(f"  {'Year':<12} {'N':>5} {'WR%':>6} {'AvgNet':>8} {'PF':>6}")
        print("  " + "-" * 40)
        for yr in sorted(trades_df["year"].unique()):
            sub = trades_df[trades_df["year"] == yr]
            m = metrics(sub.to_dict("records"))
            print(f"  {yr:<12} {m['n']:>5} {m['wr']*100:>5.1f}% {fp(m['avg']):>8} {m['pf']:>6.2f}{pf_tag(m['pf'])}")

    # ════════════════════════════════════════════════════
    # D. REGIME-GATED ENTRIES (best strategy)
    # ════════════════════════════════════════════════════
    best_s     = top3[0]
    need_rs    = best_s in NEEDS_RS_PCT
    rs_for_run = rs_pct if need_rs else None

    print("\n" + "=" * 70)
    print(f"D. REGIME-GATED ENTRIES  ({best_s}, Policy B, 5d)")
    print("=" * 70)
    print(_hdr())
    print(_sep())

    gate_configs = [
        ("All regimes",       None),
        ("BULL+NEUTRAL",      {"BULL", "NEUTRAL"}),
        ("BULL+NEUTRAL+WEAK", {"BULL", "NEUTRAL", "WEAK"}),
        ("BULL only",         {"BULL"}),
    ]
    for label, rf in gate_configs:
        tr = run_backtest(stocks, best_s, "B", max_hold=5,
                          regime_filter=rf, entry_timing="T1",
                          rs_pct_data=rs_for_run)
        m = metrics(tr)
        print(_row(label, m))

    # ════════════════════════════════════════════════════
    # E. ENTRY TIMING (best strategy, BULL+NEUTRAL, Policy B, 5d)
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print(f"E. ENTRY TIMING  ({best_s}, BULL+NEUTRAL, Policy B, 5d)")
    print("=" * 70)
    print(_hdr())
    print(_sep())

    for timing, label in [
        ("T1", "next-day open"),
        ("T2", "next-day close > SMA5"),
        ("T3", "next-day close > prev-high"),
    ]:
        tr = run_backtest(stocks, best_s, "B", max_hold=5,
                          regime_filter={"BULL", "NEUTRAL"}, entry_timing=timing,
                          rs_pct_data=rs_for_run)
        m = metrics(tr)
        print(_row(f"T{timing[-1]}: {label}", m))

    # ════════════════════════════════════════════════════
    # F. EXIT POLICY on best entry
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print(f"F. EXIT POLICY  ({best_s}, BULL+NEUTRAL, best entry timing)")
    print("=" * 70)
    print(_hdr())
    print(_sep())

    for pol in ["B", "D", "C"]:
        for hold in [5, 10]:
            tr = run_backtest(stocks, best_s, pol, max_hold=hold,
                              regime_filter={"BULL", "NEUTRAL"}, entry_timing="T1",
                              rs_pct_data=rs_for_run)
            m = metrics(tr)
            print(_row(f"Pol {pol} hold={hold}d", m))

    # ════════════════════════════════════════════════════
    # G. COMPREHENSIVE SWEEP — all promising combos
    # ════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("G. COMPREHENSIVE SWEEP  (top RS strategies × regime × exit × timing)")
    print("=" * 70)

    sweep_strategies = ["S4_RS>=0.0%", "S5_RS>=-2%", "S6_RSTop50%", "S7_RSTop30%", "S8_RSTop30%+Vol"]
    sweep_regimes    = [
        ("all",          None),
        ("BULL+NEUTRAL", {"BULL", "NEUTRAL"}),
        ("BN+WEAK",      {"BULL", "NEUTRAL", "WEAK"}),
    ]
    sweep_policies   = ["B", "D", "C"]
    sweep_holds      = [5, 10]
    sweep_timings    = ["T1", "T3"]

    combos = []
    total  = len(sweep_strategies) * len(sweep_regimes) * len(sweep_policies) * len(sweep_holds) * len(sweep_timings)
    done   = 0

    for sn in sweep_strategies:
        nr = sn in NEEDS_RS_PCT
        for rlabel, rf in sweep_regimes:
            for pol in sweep_policies:
                for hold in sweep_holds:
                    for timing in sweep_timings:
                        done += 1
                        if done % 20 == 0:
                            print(f"  sweep {done}/{total}...", flush=True)
                        tr = run_backtest(stocks, sn, pol, max_hold=hold,
                                          regime_filter=rf, entry_timing=timing,
                                          rs_pct_data=rs_pct if nr else None)
                        m = metrics(tr)
                        key = f"{sn}|{rlabel}|Pol{pol}|{hold}d|{timing}"
                        combos.append((m["pf"], m["n"], key, m))

    combos.sort(reverse=True)

    print(f"\n  Top 20 combinations (min 15 trades):")
    print(f"  {'Combination':<58} {'N':>5} {'PF':>6} {'WR%':>6} {'AvgNet':>8}")
    print("  " + "-" * 90)
    shown = 0
    for pf_v, n_v, key, m in combos:
        if n_v < 15:
            continue
        mark = pf_tag(pf_v)
        print(f"  {key:<58} {n_v:>5} {pf_v:>6.2f} {m['wr']*100:>5.1f}% {fp(m['avg']):>8}{mark}")
        shown += 1
        if shown >= 20:
            break

    # ════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ════════════════════════════════════════════════════
    total_elapsed = time.time() - t0
    print(f"\n{'=' * 70}")
    print(f"PHASE 1.5 COMPLETE  — Total runtime: {total_elapsed:.0f}s")
    print(f"{'=' * 70}")

    viable = [(pf_v, n_v, key, m) for pf_v, n_v, key, m in combos if n_v >= 20]
    if viable and viable[0][0] >= 1.10:
        pf_v, n_v, key, m = viable[0]
        print(f"\n  PF > 1.10 ACHIEVED")
        print(f"  Best config  : {key}")
        print(f"  Trades       : {n_v}")
        print(f"  Win rate     : {m['wr']*100:.1f}%")
        print(f"  Avg net PnL  : {fp(m['avg'])}")
        print(f"  Profit factor: {pf_v:.2f}")
        print(f"  Stop rate    : {m['stop_r']*100:.1f}%")
        print(f"  Avg winner   : {fp(m['avg_w'])}")
        print(f"  Avg loser    : {fp(m['avg_l'])}")
        print(f"  Avg hold     : {m['avg_hold']:.1f}d")
    else:
        best = next(((pf_v, n_v, key, m) for pf_v, n_v, key, m in combos if n_v >= 20), None)
        if best:
            pf_v, n_v, key, m = best
            print(f"\n  PF > 1.10 NOT achieved.")
            print(f"  Best found   : {key}")
            print(f"  Best PF      : {pf_v:.2f}  (target: 1.10)")
            print(f"  Trades       : {n_v}, Win rate: {m['wr']*100:.1f}%")
        else:
            print("\n  No viable combination found (all have < 20 trades).")


if __name__ == "__main__":
    main()
