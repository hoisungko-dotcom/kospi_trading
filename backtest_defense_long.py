"""
DEFENSE_LONG 전략 백테스트 — sma224 장기 추세 필터 추가 효과 검증

현재 전략(sma60까지만 봄) vs 신규 전략(sma224 위에서만 진입) 비교.

사용법:
  python backtest_defense_long.py             # KOSPI 상위 50종목, 2년
  python backtest_defense_long.py --days 504  # 2년
  python backtest_defense_long.py --top 100   # 상위 100종목
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

try:
    import FinanceDataReader as fdr
except ImportError:
    print("FinanceDataReader 없음. pip install finance-datareader")
    sys.exit(1)


# ── 지표 계산 ─────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    close  = df["close"].astype(float)
    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    volume = df["volume"].astype(float)

    df["sma5"]       = close.rolling(5).mean()
    df["sma20"]      = close.rolling(20).mean()
    df["sma60"]      = close.rolling(60).mean()
    df["sma224"]     = close.rolling(224).mean()

    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    df["avg_volume20"] = volume.rolling(20).mean()
    df["return5"]      = close.pct_change(5)
    df["return20"]     = close.pct_change(20)
    df["high20"]       = high.rolling(20).max()

    tr = pd.concat([
        high - low,
        abs(high - close.shift()),
        abs(low  - close.shift()),
    ], axis=1).max(axis=1)
    df["atr"]     = tr.rolling(14).mean()
    df["atr_pct"] = df["atr"] / close

    df["open"]  = df["open"].astype(float)
    df["high"]  = high
    df["low"]   = low
    return df


# ── DEFENSE_LONG 조건 ────────────────────────────────────────────────────

def is_defense_long(row: pd.Series, use_sma224: bool) -> bool:
    """v4.3 signal_engine._strategy_for WEAK 분기와 동일한 조건."""
    try:
        close        = float(row["close"])
        sma5         = float(row["sma5"])
        sma20        = float(row["sma20"])
        sma60        = float(row["sma60"])
        sma224       = float(row["sma224"])
        rsi          = float(row["rsi14"])
        return5      = float(row["return5"])
        return20     = float(row["return20"])
        atr_pct      = float(row["atr_pct"])
        avg_vol      = float(row["avg_volume20"])
        vol          = float(row["volume"])
        volume_ratio = vol / max(avg_vol, 1)
    except (KeyError, TypeError, ValueError):
        return False

    if any(np.isnan(v) for v in [sma5, sma20, sma60, sma224, rsi, return5, return20, atr_pct]):
        return False

    base = (
        close > sma20
        and close >= sma5 * 0.995
        and sma20 >= sma60 * 0.98
        and -0.08 <= return5 <= 0.06
        and -0.05 <= return20 <= 0.35
        and 42 <= rsi <= 64
        and atr_pct <= 0.08
        and volume_ratio >= 0.65
    )
    if not base:
        return False
    if use_sma224 and close <= sma224:
        return False
    return True


# ── 백테스트 엔진 ─────────────────────────────────────────────────────────

STOP_LOSS    = -0.020   # -2%
TAKE_PROFIT  =  0.050   # +5%
COST         =  0.0035  # 왕복 거래비용
MAX_HOLD     =  5       # 최대 보유일


def simulate(symbol: str, df: pd.DataFrame, use_sma224: bool) -> list[dict]:
    trades = []
    df = df.reset_index(drop=True)
    i = 0
    while i < len(df) - 1:
        row = df.iloc[i]
        if not is_defense_long(row, use_sma224):
            i += 1
            continue

        # 다음날 시가 진입
        entry_i = i + 1
        if entry_i >= len(df):
            break
        entry_price = float(df.iloc[entry_i]["open"])
        if entry_price <= 0:
            i += 1
            continue

        stop_price   = entry_price * (1 + STOP_LOSS)
        target_price = entry_price * (1 + TAKE_PROFIT)
        exit_price   = None
        exit_reason  = None

        for j in range(entry_i, min(entry_i + MAX_HOLD, len(df))):
            day = df.iloc[j]
            low_d  = float(day["low"])
            high_d = float(day["high"])
            close_d = float(day["close"])

            if low_d <= stop_price:
                exit_price  = stop_price
                exit_reason = "stop"
                break
            if high_d >= target_price:
                exit_price  = target_price
                exit_reason = "take"
                break
            if j == min(entry_i + MAX_HOLD, len(df)) - 1:
                exit_price  = close_d
                exit_reason = "time"
                break

        if exit_price is None:
            i += 1
            continue

        gross_pnl = exit_price / entry_price - 1
        net_pnl   = gross_pnl - COST

        trades.append({
            "symbol":      symbol,
            "signal_date": str(df.iloc[i]["date"]) if "date" in df.columns else str(i),
            "entry_price": round(entry_price, 0),
            "exit_price":  round(exit_price, 0),
            "gross_pnl":   round(gross_pnl * 100, 2),
            "net_pnl":     round(net_pnl * 100, 2),
            "exit_reason": exit_reason,
            "sma224_filter": use_sma224,
        })
        i = entry_i + (j - entry_i) + 1  # 청산일 다음부터 재탐색

    return trades


# ── 결과 출력 ─────────────────────────────────────────────────────────────

def print_summary(label: str, trades: list[dict]) -> None:
    if not trades:
        print(f"\n[{label}] 거래 없음\n")
        return

    pnls = [t["net_pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    stops  = sum(1 for t in trades if t["exit_reason"] == "stop")
    takes  = sum(1 for t in trades if t["exit_reason"] == "take")
    times  = sum(1 for t in trades if t["exit_reason"] == "time")

    print(f"\n{'='*55}")
    print(f"  {label}")
    print(f"{'='*55}")
    print(f"  총 거래수    : {len(trades)}건")
    print(f"  승률         : {len(wins)/len(trades)*100:.1f}%  ({len(wins)}승 {len(losses)}패)")
    print(f"  평균 순수익  : {np.mean(pnls):.2f}%")
    print(f"  평균 수익 (승): {np.mean(wins):.2f}%") if wins else None
    print(f"  평균 손실 (패): {np.mean(losses):.2f}%") if losses else None
    print(f"  손익비       : {abs(np.mean(wins)/np.mean(losses)):.2f}x") if wins and losses else None
    print(f"  최대 손실    : {min(pnls):.2f}%")
    print(f"  최대 수익    : {max(pnls):.2f}%")
    print(f"  누적 순수익  : {sum(pnls):.2f}%")
    print(f"  청산 분류    : 손절={stops} / 익절={takes} / 시간={times}")
    print(f"{'='*55}")

    # 손절 많은 종목 top5
    from collections import Counter
    stop_by_sym = Counter(t["symbol"] for t in trades if t["exit_reason"] == "stop")
    if stop_by_sym:
        print("  손절 상위:")
        for sym, cnt in stop_by_sym.most_common(5):
            print(f"    {sym}: {cnt}회")


# ── 메인 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=504, help="백테스트 기간(거래일, 기본=504=2년)")
    parser.add_argument("--top",  type=int, default=50,  help="KOSPI 시총 상위 N개 (기본=50)")
    args = parser.parse_args()

    end   = datetime.today()
    start = end - timedelta(days=int(args.days * 1.5) + 500)  # sma224 계산용 여유

    print(f"\n📡 KOSPI 시총 상위 {args.top}종목 다운로드 중...")
    try:
        listing = fdr.StockListing("KOSPI")
        listing = listing.sort_values("Marcap", ascending=False).head(args.top)
        symbols = listing["Code"].tolist()
        names   = dict(zip(listing["Code"], listing["Name"]))
    except Exception as e:
        print(f"종목 목록 실패: {e}")
        sys.exit(1)

    all_current = []
    all_filtered = []
    skipped = 0

    for sym in symbols:
        try:
            raw = fdr.DataReader(sym, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            if raw is None or len(raw) < 250:
                skipped += 1
                continue
            raw = raw.reset_index()
            raw.columns = [c.lower() for c in raw.columns]
            raw = raw.rename(columns={"date": "date"})
            df = compute_indicators(raw)

            # 백테스트 기간만 추출 (sma224 계산 후)
            cutoff = end - timedelta(days=args.days)
            df_bt = df[df["date"] >= pd.Timestamp(cutoff)].copy()
            if len(df_bt) < 10:
                skipped += 1
                continue

            df_bt["symbol"] = sym
            df_bt["name"]   = names.get(sym, sym)

            t_curr   = simulate(sym, df_bt, use_sma224=False)
            t_filter = simulate(sym, df_bt, use_sma224=True)
            all_current.extend(t_curr)
            all_filtered.extend(t_filter)

        except Exception as e:
            skipped += 1
            continue

    print(f"✅ 완료 (스킵: {skipped}/{args.top})")
    print(f"   백테스트 기간: {(end - timedelta(days=args.days)).strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}")

    print_summary("현재 전략 (sma60까지)", all_current)
    print_summary("신규 전략 (sma224 위에서만 진입)", all_filtered)

    # 차이 요약
    if all_current and all_filtered:
        print(f"\n{'─'*55}")
        print(f"  비교 요약")
        print(f"{'─'*55}")
        diff_trades = len(all_current) - len(all_filtered)
        diff_winrate = (
            len([t for t in all_filtered if t["net_pnl"] > 0]) / len(all_filtered) * 100
            - len([t for t in all_current if t["net_pnl"] > 0]) / len(all_current) * 100
        )
        diff_avg = np.mean([t["net_pnl"] for t in all_filtered]) - np.mean([t["net_pnl"] for t in all_current])
        print(f"  거래 감소   : -{diff_trades}건 ({diff_trades/len(all_current)*100:.1f}% 필터됨)")
        print(f"  승률 변화   : {diff_winrate:+.1f}%p")
        print(f"  평균 수익 변화: {diff_avg:+.2f}%p")
        print(f"{'─'*55}\n")

        # sma224 필터로 걸러진 종목 샘플
        sym_current  = set(t["symbol"] for t in all_current)
        sym_filtered = set(t["symbol"] for t in all_filtered)
        blocked = sym_current - sym_filtered
        if blocked:
            print(f"  sma224 필터에 막힌 종목 ({len(blocked)}개):")
            for s in sorted(blocked)[:15]:
                print(f"    {s} {names.get(s, '')}")


if __name__ == "__main__":
    main()
