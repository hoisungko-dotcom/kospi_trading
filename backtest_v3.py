"""
한국주식 v3 전략 백테스트
FinanceDataReader 일봉 데이터로 v3 SignalEngine 검증

사용법:
  python backtest_v3.py                  # 기본 종목 6개월
  python backtest_v3.py --days 252       # 1년
  python backtest_v3.py --top 50         # KOSPI 시총 상위 50종목
  python backtest_v3.py --universe mixed # KOSPI 11~210위 + KOSDAQ 상위 50
  python backtest_v3.py --cost 0.40      # 왕복 거래비용 0.40% 반영 (기본값)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from core.signal import SignalEngine
from core.regime import Regime

try:
    import FinanceDataReader as fdr
    from rich.console import Console
    from rich.table import Table
    console = Console()
    USE_RICH = True
except ImportError:
    USE_RICH = False
    class Console:
        def print(self, *a, **k): print(*a)
    console = Console()

engine = SignalEngine()

_ENTRY_TYPES = ["LONG_MA_BREAK", "BREAKOUT", "PULLBACK", "MOMENTUM", "REVERSAL"]
_GRADES      = {"A": 1.0, "B": 0.7, "C": 0.3, "D": 0.0}


# ── 지표 계산 ────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    close  = df["close"].astype(float)
    high   = df["high"].astype(float)
    volume = df["volume"].astype(float)

    df["sma_5"]   = close.rolling(5).mean()
    df["sma_20"]  = close.rolling(20).mean()
    df["sma_60"]  = close.rolling(60).mean()
    df["sma_224"] = close.rolling(224).mean()
    df["sma_448"] = close.rolling(448).mean()
    df["sma_224_prev"] = df["sma_224"].shift(1)
    df["sma_20_prev"]  = df["sma_20"].shift(1)
    df["close_prev"]   = close.shift(1)

    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"]      = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    df["rsi_prev"] = df["rsi"].shift(1)

    ema12              = close.ewm(span=12, adjust=False).mean()
    ema26              = close.ewm(span=26, adjust=False).mean()
    df["macd"]         = ema12 - ema26
    df["macd_signal"]  = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_prev"]    = df["macd"].shift(1)
    df["macd_signal_prev"] = df["macd_signal"].shift(1)

    df["bb_middle"] = close.rolling(20).mean()
    df["bb_upper"]  = df["bb_middle"] + 2 * close.rolling(20).std()

    df["avg_volume_20"] = volume.rolling(20).mean()
    df["close_5d_ago"]  = close.shift(5)
    df["close_20d_ago"] = close.shift(20)
    df["high_20d"]      = high.rolling(20).max()
    df["prev_high"]     = high.shift(1)

    open_p = df["open"].astype(float) if "open" in df.columns else close
    buy_day = ((close > open_p) & (volume > df["avg_volume_20"] * 1.1)).astype(int)
    cbd = []
    count = 0
    for v in buy_day:
        count = count + 1 if v else 0
        cbd.append(count)
    df["consecutive_buy_days"] = cbd

    return df


# ── 시장 국면 ────────────────────────────────────────────────────────────

def build_kospi_regime_series(start: str, end: str) -> pd.Series:
    try:
        df = fdr.DataReader("KS11",
                            (pd.Timestamp(start) - timedelta(days=100)).strftime("%Y-%m-%d"),
                            end)
        close = df["Close"].astype(float)
        high  = df["High"].astype(float)
        low   = df["Low"].astype(float)
        sma20 = close.rolling(20).mean()
        tr    = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean()
        vol   = atr14 / close * 100
        trend_gap = (close - sma20) / sma20 * 100

        def classify(row):
            if row["close"] >= row["sma20"]:
                if row["vol"] <= 2.5:
                    return Regime.STRONG if row["gap"] >= 5.0 else Regime.NORMAL
                return Regime.CAUTION
            return Regime.CAUTION

        tmp = pd.DataFrame({
            "close": close, "sma20": sma20,
            "vol": vol, "gap": trend_gap,
        }).dropna()
        return tmp.apply(classify, axis=1)
    except Exception as e:
        console.print(f"[yellow]KOSPI 국면 계산 실패 — NORMAL 고정: {e}[/yellow]")
        return pd.Series(dtype=object)


# ── 진입 유형 분류 ────────────────────────────────────────────────────────

def classify_entry_type(row: dict, cur_price: float) -> str:
    sma_20       = float(row.get("sma_20", 0) or 0)
    sma_60       = float(row.get("sma_60", 0) or 0)
    sma_224      = float(row.get("sma_224", 0) or 0)
    sma_448      = float(row.get("sma_448", 0) or 0)
    sma_224_prev = float(row.get("sma_224_prev", 0) or 0)
    close_prev   = float(row.get("close_prev", cur_price) or cur_price)
    high_20d     = float(row.get("high_20d", 0) or 0)
    volume       = float(row.get("volume", 0) or 0)
    avg_volume   = float(row.get("avg_volume_20", volume) or volume)
    prev5        = float(row.get("close_5d_ago", cur_price) or cur_price)
    cbd          = int(row.get("consecutive_buy_days", 0) or 0)

    vol_surge = avg_volume > 0 and volume >= avg_volume * 1.5
    ret5d     = (cur_price - prev5) / prev5 * 100 if prev5 > 0 else 0

    if ((sma_224 > 0 and cur_price > sma_224 and close_prev <= sma_224_prev)
            or (sma_448 > 0 and cur_price > sma_448 and close_prev <= sma_448)):
        return "LONG_MA_BREAK"
    if high_20d > 0 and cur_price >= high_20d * 0.995 and vol_surge:
        return "BREAKOUT"
    if sma_20 > 0 and sma_60 > 0 and sma_20 > sma_60 and ret5d < -1.0:
        return "PULLBACK"
    if cbd >= 3:
        return "MOMENTUM"
    return "REVERSAL"


# ── 신호 등급 ─────────────────────────────────────────────────────────────

def grade_signal(score: float, has_penalty: bool) -> tuple[str, float]:
    """A/B/C/D 등급과 포지션 배율 반환.

    A: 비중 1.0 / B: 0.7 / C: 0.3 (관찰 수준) / D: 0.0 (진입 금지)
    """
    if has_penalty:          # 연속 손절 패널티
        if score >= 85:
            return "B", 0.7  # 매우 강한 신호면 축소 허용
        return "D", 0.0

    if score >= 85:
        return "A", 1.0
    elif score >= 75:
        return "B", 0.7
    elif score >= 65:
        return "C", 0.3
    return "D", 0.0          # should_buy 통과했지만 낮은 점수 — 이론상 발생 안 함


# ── BacktestResult ────────────────────────────────────────────────────────

class BacktestResult:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.trades: list[dict] = []

    def add_trade(
        self,
        entry_date, exit_date,
        entry_price, exit_price,
        reason,
        weight: float = 1.0,
        entry_type: str = "",
        mfe: float = 0.0,
        mae: float = 0.0,
        grade: str = "B",
        pos_weight: float = 1.0,
        cost_pct: float = 0.0,
    ):
        raw_pnl  = (exit_price - entry_price) / entry_price * 100
        net_pnl  = raw_pnl - cost_pct           # 비용 차감
        hold_days = (exit_date - entry_date).days if hasattr(exit_date, 'days') else 0
        try:
            hold_days = (pd.Timestamp(exit_date) - pd.Timestamp(entry_date)).days
        except Exception:
            hold_days = 0

        self.trades.append({
            "entry": entry_date, "exit": exit_date,
            "entry_p": entry_price, "exit_p": exit_price,
            "pnl": net_pnl, "raw_pnl": raw_pnl,
            "reason": reason, "weight": weight,
            "entry_type": entry_type, "mfe": mfe, "mae": mae,
            "grade": grade, "pos_weight": pos_weight,
            "hold_days": hold_days, "cost_pct": cost_pct,
        })

    @property
    def n(self): return len(self.trades)

    @property
    def win_rate(self):
        if not self.trades: return 0
        total_w = sum(t.get("weight", 1.0) for t in self.trades)
        win_w   = sum(t.get("weight", 1.0) for t in self.trades if t["pnl"] > 0)
        return win_w / total_w if total_w > 0 else 0

    @property
    def avg_pnl(self):
        if not self.trades: return 0
        total_w = sum(t.get("weight", 1.0) for t in self.trades)
        return sum(t["pnl"] * t.get("weight", 1.0) for t in self.trades) / total_w

    @property
    def total_pnl(self):
        r = 1.0
        for t in self.trades:
            w = t.get("weight", 1.0)
            r *= (1 + t["pnl"] / 100) ** w
        return (r - 1) * 100


# ── 단일 종목 백테스트 ────────────────────────────────────────────────────

def backtest_symbol(
    symbol: str,
    df_raw: pd.DataFrame,
    regime_s: pd.Series,
    stop_loss_pct: float = -3.0,
    trailing_gap_pct: float = 5.0,
    take_profit_pct: float = 15.0,
    surge_block_pct: float = 12.0,
    partial_profit_pct: float = 8.0,
    breakeven_trigger_pct: float = 5.0,
    sma20_max_dev_pct: float = 12.0,
    rsi_max: float = 82.0,
    cost_pct: float = 0.40,      # 왕복 거래비용 (수수료 + 세금 + 슬리피지)
) -> BacktestResult:
    result = BacktestResult(symbol)
    if df_raw is None or len(df_raw) < 60:
        return result

    df = compute_indicators(df_raw)
    df = df.dropna(subset=["sma_20", "rsi", "macd"])

    from core.regime import MarketSnapshot
    def get_snap(date) -> MarketSnapshot:
        r = Regime.NORMAL
        if len(regime_s) > 0:
            idx = regime_s.index[regime_s.index <= date]
            if len(idx) > 0:
                r = regime_s.loc[idx[-1]]
        return MarketSnapshot(
            regime=r, kospi_close=0, kospi_sma20=0,
            trend_gap_pct=0, vol_pct=0,
            usdkrw=None, usdkrw_5d_chg=None, frgn_trend=None,
        )

    # ── 쿨다운 상태 (최근 거래 기반) ────────────────────────────────────
    cooldown: dict = {
        "until":         None,
        "score_penalty": 0.0,
        "history":       [],
    }
    def record_exit(exit_date, is_sl: bool) -> None:
        cooldown["history"].append({"date": exit_date, "is_sl": is_sl})
        h = cooldown["history"]

        # 연속 손절 2회 → 다음 진입 +10점 요구
        cooldown["score_penalty"] = (
            10.0 if len(h) >= 2 and h[-1]["is_sl"] and h[-2]["is_sl"] else 0.0
        )

        if not is_sl:
            return

        # 승률 0% + 손절 3회 → 60거래일(≈84 캘린더일) 제외
        if all(t["is_sl"] for t in h) and len(h) >= 3:
            cooldown["until"] = exit_date + pd.Timedelta(days=84)
            return

        # 최근 3거래 중 2회 손절 → 10거래일(≈14 캘린더일) 쿨다운
        if sum(1 for t in h[-3:] if t["is_sl"]) >= 2:
            cooldown["until"] = exit_date + pd.Timedelta(days=14)
            return

        # 최근 5거래 중 3회 손절 → 30거래일(≈42 캘린더일) 쿨다운
        if sum(1 for t in h[-5:] if t["is_sl"]) >= 3:
            cooldown["until"] = exit_date + pd.Timedelta(days=42)

    def is_cooling_down(cur_date) -> bool:
        return cooldown["until"] is not None and cur_date <= cooldown["until"]

    holding = None

    for i in range(60, len(df)):
        row        = df.iloc[i]
        cur_price  = float(row["close"])
        cur_date   = df.index[i]
        price_data = row.to_dict()

        if holding:
            # ── 매도 체크 ──────────────────────────────────────────────
            buy_price = holding["entry_price"]
            holding["highest"] = max(holding["highest"], cur_price)
            holding["lowest"]  = min(holding["lowest"],  cur_price)

            profit    = (cur_price - buy_price) / buy_price * 100
            mfe       = (holding["highest"] - buy_price) / buy_price * 100
            mae       = (holding["lowest"]  - buy_price) / buy_price * 100
            reason    = ""
            is_sl     = False

            # 본전 스탑 활성화
            if not holding.get("breakeven_active") and profit >= breakeven_trigger_pct:
                holding["breakeven_active"] = True
            effective_stop = 0.0 if holding.get("breakeven_active") else stop_loss_pct

            # 부분 익절: +8% 도달 시 30% 청산
            if (partial_profit_pct > 0
                    and not holding.get("partial_done")
                    and profit >= partial_profit_pct):
                result.add_trade(
                    holding["entry_date"], cur_date,
                    buy_price, cur_price, f"부분익절 {profit:.1f}%",
                    weight=0.3,
                    entry_type=holding["entry_type"],
                    mfe=mfe, mae=mae,
                    grade=holding["grade"],
                    pos_weight=holding["pos_weight"],
                    cost_pct=cost_pct,
                )
                holding["partial_done"] = True

            if profit <= effective_stop:
                reason = "손절(본전)" if holding.get("breakeven_active") else f"손절 {profit:.1f}%"
                is_sl  = not holding.get("breakeven_active")
            elif profit >= take_profit_pct:
                reason = f"익절 {profit:.1f}%"
            elif holding["highest"] > buy_price:
                trail = holding["highest"] * (1 - trailing_gap_pct / 100)
                if cur_price < trail:
                    mp = (holding["highest"] - buy_price) / buy_price * 100
                    reason = f"트레일링 (최고 +{mp:.1f}%)"
            elif float(row.get("rsi", 50)) <= 30:
                reason = "RSI과매도"

            if reason:
                w = 0.7 if holding.get("partial_done") else 1.0
                result.add_trade(
                    holding["entry_date"], cur_date,
                    buy_price, cur_price, reason,
                    weight=w,
                    entry_type=holding["entry_type"],
                    mfe=mfe, mae=mae,
                    grade=holding["grade"],
                    pos_weight=holding["pos_weight"],
                    cost_pct=cost_pct,
                )
                record_exit(cur_date, is_sl)
                holding = None
            continue

        # ── 매수 체크 ─────────────────────────────────────────────────
        snap = get_snap(cur_date)

        if snap.regime == Regime.BLOCKED:
            continue
        if is_cooling_down(cur_date):
            continue

        # 5일 급등 차단
        prev5 = float(row.get("close_5d_ago", cur_price) or cur_price)
        if prev5 > 0 and (cur_price - prev5) / prev5 * 100 >= surge_block_pct:
            continue

        # SMA20 과도 이격 차단
        sma_20 = float(row.get("sma_20", 0) or 0)
        if sma_20 > 0 and cur_price > sma_20 * (1 + sma20_max_dev_pct / 100):
            continue

        # RSI 과열 차단
        rsi = float(row.get("rsi", 50) or 50)
        if rsi >= rsi_max:
            continue

        # MA20 기울기 ≤ 0 차단
        sma_20_prev = float(row.get("sma_20_prev", 0) or 0)
        if sma_20 > 0 and sma_20_prev > 0 and sma_20 <= sma_20_prev:
            continue

        ok, _ = engine.should_buy(symbol, price_data, snap)
        if not ok:
            continue

        # 신호 등급 및 포지션 배율 결정
        sc = engine.score(symbol, price_data)
        has_penalty = cooldown["score_penalty"] > 0
        if has_penalty:
            base_thr = snap.score_threshold(float(os.getenv("SIGNAL_NORMAL_SCORE", "58")))
            if sc < base_thr + cooldown["score_penalty"]:
                continue

        g, pw = grade_signal(sc, has_penalty)
        if pw == 0.0:  # D등급 진입 금지
            continue

        entry_type = classify_entry_type(row.to_dict(), cur_price)

        # ── v4: 주력 BREAKOUT·MOMENTUM, 보조 PULLBACK만 허용 ──────────────────
        if entry_type in {"REVERSAL", "LONG_MA_BREAK"}:
            continue

        # PULLBACK: C등급 차단, A/B만 허용
        if entry_type == "PULLBACK":
            if g == "C":
                continue
            pw = min(pw, 0.7)

        # B-BREAKOUT: 0.5x, 시장+과열 필터
        if entry_type == "BREAKOUT" and g == "B":
            bk_rsi  = float(price_data.get("rsi", 50) or 50)
            bk_high = float(price_data.get("high", cur_price) or cur_price)
            bk_wick = (bk_high - cur_price) / cur_price if cur_price > 0 else 0
            # 시장 CAUTION / 종목 20일선 아래 / RSI 78+ / 장대 윗꼬리(3%+) → 차단
            if (snap.regime == Regime.CAUTION
                    or (sma_20 > 0 and cur_price <= sma_20)
                    or bk_rsi >= 78
                    or bk_wick > 0.03):
                continue
            pw = min(pw, 0.5)

        # B-MOMENTUM: 0.5x, 시장+종목 필터
        if entry_type == "MOMENTUM" and g == "B":
            bm_rsi = float(price_data.get("rsi", 50) or 50)
            # 시장 CAUTION / 종목이 20일선 아래 / RSI 과열 → 차단
            if (snap.regime == Regime.CAUTION
                    or (sma_20 > 0 and cur_price <= sma_20)
                    or bm_rsi >= 75):
                continue
            pw = min(pw, 0.5)

        # CAUTION 국면: 포지션 절반 (지수 20일선 이탈 구간 방어)
        if snap.regime == Regime.CAUTION:
            pw = min(pw, 0.5)

        holding = {
            "entry_price":      cur_price,
            "highest":          cur_price,
            "lowest":           cur_price,
            "entry_date":       cur_date,
            "entry_type":       entry_type,
            "grade":            g,
            "pos_weight":       pw,
            "breakeven_active": False,
            "partial_done":     False,
        }

    if holding:
        last_price = float(df.iloc[-1]["close"])
        mfe = (holding["highest"] - holding["entry_price"]) / holding["entry_price"] * 100
        mae = (holding["lowest"]  - holding["entry_price"]) / holding["entry_price"] * 100
        result.add_trade(
            holding["entry_date"], df.index[-1],
            holding["entry_price"], last_price, "기간종료",
            entry_type=holding["entry_type"], mfe=mfe, mae=mae,
            grade=holding["grade"], pos_weight=holding["pos_weight"],
            cost_pct=cost_pct,
        )

    return result


# ── 포트폴리오 MDD ─────────────────────────────────────────────────────────

def compute_portfolio_mdd(all_trades: list[dict]) -> tuple[float, list]:
    """모든 거래를 시간순으로 정렬해 복리 적용 후 MDD 계산."""
    sorted_trades = sorted(all_trades, key=lambda t: t["exit"])
    equity = 100.0
    curve  = [equity]
    peak   = equity
    max_dd = 0.0
    for t in sorted_trades:
        w  = t.get("weight", 1.0) * t.get("pos_weight", 1.0)
        equity *= (1 + t["pnl"] / 100 * w)
        curve.append(equity)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
    return max_dd, curve


# ── 전체 실행 ─────────────────────────────────────────────────────────────

def run(
    symbols: list[str],
    days: int = 180,
    stop_loss_pct: float = -3.0,
    trailing_gap_pct: float = 5.0,
    take_profit_pct: float = 15.0,
    partial_profit_pct: float = 8.0,
    min_turnover: float = 3e9,
    cost_pct: float = 0.40,
) -> None:
    end   = datetime.today()
    start = end - timedelta(days=days + 500)

    console.print(f"\n[bold cyan]📊 KospiBot v3 백테스트[/bold cyan]")
    console.print(f"  기간: {(end - timedelta(days=days)).strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}")
    console.print(f"  종목: {len(symbols)}개 | 왕복 거래비용: {cost_pct:.2f}%\n")

    console.print("  KOSPI 국면 시리즈 계산 중...")
    regime_s = build_kospi_regime_series(
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    )

    all_results: list[BacktestResult] = []
    failed = 0

    for i, sym in enumerate(symbols, 1):
        try:
            df_raw = fdr.DataReader(sym, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            if df_raw is None or len(df_raw) < 100:
                failed += 1
                continue

            if min_turnover > 0:
                df_tmp = df_raw.copy()
                df_tmp.columns = [c.lower() for c in df_tmp.columns]
                if "close" in df_tmp.columns and "volume" in df_tmp.columns:
                    turnover = (df_tmp["close"].astype(float) * df_tmp["volume"].astype(float)).rolling(20).mean().iloc[-1]
                    if turnover < min_turnover:
                        failed += 1
                        console.print(f"  [{i:3d}/{len(symbols)}] {sym}  ⏭ 거래대금 부족 ({turnover/1e8:.0f}억)")
                        continue

            res = backtest_symbol(
                df_raw=df_raw, symbol=sym, regime_s=regime_s,
                stop_loss_pct=stop_loss_pct,
                trailing_gap_pct=trailing_gap_pct,
                take_profit_pct=take_profit_pct,
                partial_profit_pct=partial_profit_pct,
                cost_pct=cost_pct,
            )
            all_results.append(res)
            status = f"✅ {res.n}거래 | 승률 {res.win_rate:.0%} | 누적 {res.total_pnl:+.1f}%"
        except Exception as e:
            status = f"❌ {e}"
            failed += 1
        console.print(f"  [{i:3d}/{len(symbols)}] {sym}  {status}")

    _print_summary(all_results, failed, cost_pct)


_GRADE_SCORE = {"A": 95, "B": 80, "C": 65}


def _simulate_scenario(
    all_trades: list[dict],
    max_positions: int = 8,
    mode: str = "A",
) -> tuple:
    """포트폴리오 시뮬레이션 단일 시나리오.

    mode A: 고정 max_positions
    mode B: 최근 5일 신호 밀도에 따라 4~10 가변 (룩어헤드 없음)
    mode C: B + 등급 기반 우선순위 교체 (A가 C를 밀어냄)

    회계 정확성:
    - selected dict로 확정된 포지션만 관리
    - 교체 시 removed 포지션은 selected에서 즉시 삭제 → equity 반영 안 됨
    - peak=100.0 초기화로 초기 손실 drawdown 정확 포착
    """
    trades_sorted = sorted(
        all_trades,
        key=lambda t: (t["entry"], -_GRADE_SCORE.get(t.get("grade", "C"), 65)),
    )

    # 5일 신호 밀도 (mode B/C: 룩어헤드 없는 rolling count)
    daily_cnt: dict = {}
    for t in trades_sorted:
        d = pd.Timestamp(t["entry"]).date()
        daily_cnt[d] = daily_cnt.get(d, 0) + 1

    def get_slots(entry_dt: pd.Timestamp) -> int:
        if mode == "A":
            return max_positions
        window = entry_dt - pd.Timedelta(days=5)
        recent = sum(cnt for d, cnt in daily_cnt.items()
                     if window.date() <= d < entry_dt.date())
        if recent >= 8:
            return min(max_positions + 2, 10)
        if recent <= 2:
            return max(max_positions - 4, 4)
        return max_positions

    # selected: pos_id -> (exit_dt, port_pnl)
    # 교체로 제거된 포지션은 del selected[pos_id]로 즉시 제거
    next_id = 0
    selected: dict = {}
    open_pos: list = []   # [{"exit","port_pnl","grade","entry_dt","id","symbol","entry_type","raw_pnl"}]
    skipped = replaced = 0
    replacement_log: list[dict] = []

    for t in trades_sorted:
        entry_dt   = pd.Timestamp(t["entry"])
        exit_dt    = pd.Timestamp(t["exit"])
        grade      = t.get("grade", "C")
        symbol     = t.get("symbol", "?")
        entry_type = t.get("entry_type", "?")
        raw_pnl    = t["pnl"]   # 종목 단위 net pnl
        port_pnl   = (raw_pnl * (1.0 / max_positions)
                      * t.get("weight", 1.0) * t.get("pos_weight", 1.0))

        open_pos = [p for p in open_pos if pd.Timestamp(p["exit"]) > entry_dt]
        slots    = get_slots(entry_dt)

        if len(open_pos) < slots:
            pos_id = next_id; next_id += 1
            open_pos.append({"exit": exit_dt, "port_pnl": port_pnl,
                             "grade": grade, "entry_dt": entry_dt, "id": pos_id,
                             "symbol": symbol, "entry_type": entry_type, "raw_pnl": raw_pnl})
            selected[pos_id] = (exit_dt, port_pnl)

        elif mode == "C":
            new_score = _GRADE_SCORE.get(grade, 65)
            replaceable = [
                (i, p) for i, p in enumerate(open_pos)
                if (entry_dt - pd.Timestamp(p["entry_dt"])).days >= 2
            ]
            if replaceable:
                wi, weakest = min(replaceable,
                                  key=lambda x: _GRADE_SCORE.get(x[1]["grade"], 65))
                old_score = _GRADE_SCORE.get(weakest["grade"], 65)
                if new_score >= old_score + 15:
                    remaining_days = (pd.Timestamp(weakest["exit"]) - entry_dt).days
                    replacement_log.append({
                        "date":           entry_dt,
                        "month":          entry_dt.strftime("%Y-%m"),
                        "rem_symbol":     weakest["symbol"],
                        "rem_grade":      weakest["grade"],
                        "rem_entry_type": weakest["entry_type"],
                        "rem_pnl":        weakest["raw_pnl"],
                        "rem_remaining":  remaining_days,
                        "new_symbol":     symbol,
                        "new_grade":      grade,
                        "new_entry_type": entry_type,
                        "new_pnl":        raw_pnl,
                        "score_diff":     new_score - old_score,
                    })
                    del selected[weakest["id"]]   # 교체 포지션 equity에서 제거
                    open_pos.pop(wi)
                    pos_id = next_id; next_id += 1
                    open_pos.append({"exit": exit_dt, "port_pnl": port_pnl,
                                     "grade": grade, "entry_dt": entry_dt, "id": pos_id,
                                     "symbol": symbol, "entry_type": entry_type, "raw_pnl": raw_pnl})
                    selected[pos_id] = (exit_dt, port_pnl)
                    replaced += 1
                else:
                    skipped += 1
            else:
                skipped += 1
        else:
            skipped += 1

    # 확정된 포지션만 exit 기준 정렬 후 equity 계산
    executed_exits = sorted(selected.values(), key=lambda x: x[0])
    equity = 100.0
    peak = 100.0   # 초기 자산에서 바로 하락해도 MDD 포착
    mdd = 0.0
    month_eq: dict = {}

    for exit_dt, port_pnl in executed_exits:
        equity *= (1 + port_pnl / 100)
        peak = max(peak, equity)
        mdd  = max(mdd, (peak - equity) / peak * 100)
        month_eq[exit_dt.strftime("%Y-%m")] = equity

    return equity, mdd, len(executed_exits), skipped, replaced, month_eq, replacement_log


def _print_replacement_analysis(replacement_log: list[dict]) -> None:
    """C안 교체 품질 검증 — 이벤트 상세 + 통계."""
    n = len(replacement_log)
    if not n:
        console.print("  교체 이벤트 없음")
        return

    console.print(f"\n  [bold]🔄 교체 이벤트 상세 ({n}건)[/bold]")
    console.print(
        f"  {'날짜':10s} {'제거':8s} {'등급':2s} {'타입':11s} {'제거pnl':>7s} {'잔여':>4s}  "
        f"{'신규':8s} {'등급':2s} {'타입':11s} {'신규pnl':>7s} {'점수차':>5s}"
    )
    console.print("  " + "-" * 95)
    for ev in replacement_log:
        console.print(
            f"  {ev['date'].strftime('%Y-%m-%d')}  "
            f"{ev['rem_symbol']:8s} {ev['rem_grade']:2s} {ev['rem_entry_type']:11s} "
            f"{ev['rem_pnl']:+7.1f}% {ev['rem_remaining']:4d}일  "
            f"{ev['new_symbol']:8s} {ev['new_grade']:2s} {ev['new_entry_type']:11s} "
            f"{ev['new_pnl']:+7.1f}%  +{ev['score_diff']:2d}"
        )

    # ── 집계 통계 ─────────────────────────────────────────────────────────
    rem_pnls  = [ev["rem_pnl"] for ev in replacement_log]
    new_pnls  = [ev["new_pnl"] for ev in replacement_log]
    benefits  = [n - r for n, r in zip(new_pnls, rem_pnls)]
    pos_cnt   = sum(1 for b in benefits if b > 0)
    avg_rem   = sum(rem_pnls) / n
    avg_new   = sum(new_pnls) / n
    avg_ben   = sum(benefits) / n

    rem_loss_cnt   = sum(1 for p in rem_pnls if p < 0)
    rem_profit_cnt = sum(1 for p in rem_pnls if p >= 0)

    console.print(f"\n  [bold]📊 교체 통계[/bold]")
    console.print(f"  제거 포지션 평균 pnl   : {avg_rem:+.2f}%")
    console.print(f"  신규 포지션 평균 pnl   : {avg_new:+.2f}%")
    console.print(f"  교체 net benefit 평균  : {avg_ben:+.2f}%  "
                  f"(양수 {pos_cnt}/{n}건 = {pos_cnt/n*100:.0f}%)")
    console.print(f"  제거 포지션 손익 방향  : 손실 {rem_loss_cnt}건 / 수익 {rem_profit_cnt}건")
    if rem_profit_cnt > 0:
        console.print(f"  → 수익 진행 중 포지션을 교체한 건: {rem_profit_cnt}건 ⚠️")

    # 판정
    console.print(f"\n  [bold]📋 교체 규칙 진단[/bold]")
    if pos_cnt / n >= 0.60:
        console.print(f"  benefit 양수 비율 {pos_cnt/n*100:.0f}% ≥ 60% → 교체 규칙 유효")
    else:
        console.print(f"  benefit 양수 비율 {pos_cnt/n*100:.0f}% < 60% → 교체 규칙 약함 ⚠️")
    if avg_rem > 3.0:
        console.print(f"  제거 pnl 평균 {avg_rem:+.2f}% — 교체가 과도할 수 있음 ⚠️")
    else:
        console.print(f"  제거 pnl 평균 {avg_rem:+.2f}% — 대부분 부진 포지션 교체")

    # ── 월별 benefit ──────────────────────────────────────────────────────
    month_benefit: dict[str, list[float]] = {}
    for ev, b in zip(replacement_log, benefits):
        month_benefit.setdefault(ev["month"], []).append(b)

    console.print(f"\n  [bold]월별 교체 benefit[/bold]")
    console.print(f"  {'월':8s} {'건':>3s} {'avg benefit':>12s} {'합계':>8s}")
    console.print("  " + "-" * 36)
    concentrated = []
    for ym in sorted(month_benefit):
        bs = month_benefit[ym]
        if sum(bs) > 0:
            concentrated.append(ym)
        console.print(f"  {ym}  {len(bs):3d}  {sum(bs)/len(bs):+10.2f}%  {sum(bs):+7.2f}%")

    if len(concentrated) <= 2 and all(ym >= "2026-01" for ym in concentrated):
        console.print(f"\n  ⚠️  benefit이 {concentrated}에 집중 — 과적합 위험")
    else:
        console.print(f"\n  benefit 분산 양호 — 특정 기간 쏠림 없음")

    # ── 한계 명시 ─────────────────────────────────────────────────────────
    console.print(f"\n  ⚠️  한계: 교체 시점의 중간 가격(mid-price) 미반영.")
    console.print(f"     rem_pnl / new_pnl은 '최종 청산가 기준 예정 pnl'이며,")
    console.print(f"     교체 당시 실제 실현 손익(부분청산)은 계산되지 않음.")
    console.print(f"     benefit은 방향성 참고용 — 정확한 실현 수치 아님.")


def _compare_portfolio_scenarios(all_trades: list[dict]) -> None:
    """A/B/C 시나리오 비교."""
    console.print(f"\n[bold]🏦 포트폴리오 시뮬레이션 비교[/bold] (8슬롯 기준, 종목당 12.5%)")
    scenario_results = []
    month_tables: dict = {}

    replacement_logs: dict[str, list] = {}

    for mode, label in [
        ("A", "A안: 8슬롯 고정"),
        ("B", "B안: 신호밀도별 4~10슬롯"),
        ("C", "C안: 가변+우선순위 교체"),
    ]:
        eq, mdd, execs, skipped, replaced, month_eq, rep_log = _simulate_scenario(
            all_trades, max_positions=8, mode=mode
        )
        scenario_results.append((label, eq, mdd, execs, skipped, replaced))
        month_tables[mode] = month_eq
        replacement_logs[mode] = rep_log

    console.print(f"\n  {'시나리오':26s} {'최종자산':>8s} {'MDD':>6s} {'실행':>5s} {'건너뜀':>6s}")
    console.print("  " + "-" * 56)
    for label, eq, mdd, execs, skipped, replaced in scenario_results:
        rep = f"  (교체 {replaced})" if replaced else ""
        console.print(f"  {label:26s} {eq:8.1f}  -{mdd:.1f}% {execs:5d} {skipped:6d}{rep}")

    # A vs C 월별 비교
    a_eq_map = month_tables["A"]
    c_eq_map = month_tables["C"]
    all_months = sorted(set(a_eq_map) | set(c_eq_map))
    console.print(f"\n  월별 비교 (A / C)")
    console.print(f"  {'월':8s} {'A월수익':>7s} {'A자산':>6s}  {'C월수익':>7s} {'C자산':>6s}")
    console.print("  " + "-" * 42)
    prev_a = prev_c = 100.0
    for ym in all_months:
        a_eq = a_eq_map.get(ym, prev_a)
        c_eq = c_eq_map.get(ym, prev_c)
        a_ret = (a_eq / prev_a - 1) * 100
        c_ret = (c_eq / prev_c - 1) * 100
        win = " ◀C" if c_eq > a_eq + 0.1 else (" A▶" if a_eq > c_eq + 0.1 else "   ")
        console.print(f"  {ym}  {a_ret:+6.1f}% {a_eq:6.1f}  {c_ret:+6.1f}% {c_eq:6.1f}{win}")
        prev_a = a_eq
        prev_c = c_eq

    # C안 교체 품질 검증
    console.print(f"\n{'='*60}")
    console.print("[bold]🔄 C안 교체 품질 검증[/bold]")
    console.print("=" * 60)
    _print_replacement_analysis(replacement_logs["C"])


def _print_summary(results: list[BacktestResult], failed: int, cost_pct: float = 0.40) -> None:
    tradeable  = [r for r in results if r.n > 0]
    no_trade   = [r for r in results if r.n == 0]
    all_trades = [{**t, "symbol": r.symbol} for r in results for t in r.trades]

    console.print("\n" + "=" * 60)
    console.print("[bold]📈 전체 요약[/bold]")
    console.print("=" * 60)

    if all_trades:
        wr     = sum(1 for t in all_trades if t["pnl"] > 0) / len(all_trades)
        raw_wr = sum(1 for t in all_trades if t["raw_pnl"] > 0) / len(all_trades)
        avg    = sum(t["pnl"] for t in all_trades) / len(all_trades)
        avg_raw= sum(t["raw_pnl"] for t in all_trades) / len(all_trades)
        wins   = [t["pnl"] for t in all_trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in all_trades if t["pnl"] <= 0]
        hold_d = [t.get("hold_days", 0) for t in all_trades]
        avg_hold = sum(hold_d) / len(hold_d) if hold_d else 0
        total_cost = cost_pct * len(all_trades)

        mdd, _ = compute_portfolio_mdd(all_trades)

        reasons: dict[str, int] = {}
        for t in all_trades:
            k = t["reason"].split(" ")[0]
            reasons[k] = reasons.get(k, 0) + 1

        if USE_RICH:
            tbl = Table(show_header=True, header_style="bold magenta")
            tbl.add_column("항목"); tbl.add_column("값", justify="right")
            tbl.add_row("분석 종목",        f"{len(results)}개 (실패 {failed}개)")
            tbl.add_row("거래 발생",         f"{len(tradeable)}개 종목")
            tbl.add_row("총 거래 횟수",      f"{len(all_trades)}회")
            tbl.add_row("전체 승률 (비용 후)", f"{wr:.1%}  (비용 전 {raw_wr:.1%})")
            tbl.add_row("평균 수익률 (비용 후)", f"{avg:+.2f}%  (비용 전 {avg_raw:+.2f}%)")
            tbl.add_row("평균 이익",         f"+{sum(wins)/len(wins):.2f}%"   if wins   else "N/A")
            tbl.add_row("평균 손실",         f"{sum(losses)/len(losses):.2f}%" if losses else "N/A")
            tbl.add_row("거래당 비용",       f"-{cost_pct:.2f}%")
            tbl.add_row("총 누적 비용",      f"-{total_cost:.1f}%p (전체 거래 합산)")
            tbl.add_row("포트폴리오 MDD",    f"-{mdd:.1f}%")
            tbl.add_row("평균 보유기간",     f"{avg_hold:.1f}일")
            for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
                tbl.add_row(f"  {k}", f"{v}회")
            console.print(tbl)
        else:
            print(f"총 거래: {len(all_trades)}회 | 승률: {wr:.1%} | 평균: {avg:+.2f}% | MDD: -{mdd:.1f}%")

    # ── 등급별 요약 ──────────────────────────────────────────────────────
    if all_trades:
        console.print("\n[bold]🏅 신호 등급별 통계[/bold]")
        console.print(f"{'등급':4s} {'배율':5s} {'거래':>5s} {'승률':>6s} {'평균(비용후)':>12s} {'이익':>7s} {'손실':>7s} {'비중조정후':>10s}")
        console.print("-" * 60)
        for g in ["A", "B", "C"]:
            gt = [t for t in all_trades if t.get("grade") == g]
            if not gt:
                continue
            pw_str = f"{_GRADES[g]:.1f}x"
            n   = len(gt)
            wins_g = [t["pnl"] for t in gt if t["pnl"] > 0]
            loss_g = [t["pnl"] for t in gt if t["pnl"] <= 0]
            wr_g = len(wins_g) / n
            avg_g = sum(t["pnl"] for t in gt) / n
            a_win = sum(wins_g) / len(wins_g) if wins_g else 0
            a_los = sum(loss_g) / len(loss_g) if loss_g else 0
            # 비중 반영 기대값
            ev_weighted = avg_g * _GRADES[g]
            console.print(
                f"{g:4s} {pw_str:5s} {n:5d} {wr_g:6.0%} {avg_g:+12.2f}% "
                f"{a_win:+7.2f}% {a_los:7.2f}% {ev_weighted:+10.2f}%"
            )

    # ── entry_type별 분석 ────────────────────────────────────────────────
    if all_trades:
        console.print("\n[bold]📊 진입 유형별 분석[/bold]")
        console.print(
            f"{'유형':16s} {'거래':>5s} {'승률':>6s} {'평균':>7s} {'이익':>7s} "
            f"{'손실':>7s} {'손절%':>6s} {'MFE':>7s} {'MAE':>7s} {'보유일':>6s}"
        )
        console.print("-" * 86)

        by_type: dict[str, list] = {et: [] for et in _ENTRY_TYPES}
        for t in all_trades:
            by_type.setdefault(t.get("entry_type", "REVERSAL"), []).append(t)

        for et in _ENTRY_TYPES:
            trades = by_type.get(et, [])
            if not trades:
                continue
            n      = len(trades)
            wins   = [t for t in trades if t["pnl"] > 0]
            losses = [t for t in trades if t["pnl"] <= 0]
            sl_cnt = sum(1 for t in trades if "손절" in t.get("reason", ""))
            wr     = len(wins) / n
            avg    = sum(t["pnl"] for t in trades) / n
            a_win  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0.0
            a_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
            a_mfe  = sum(t.get("mfe", 0) for t in trades) / n
            a_mae  = sum(t.get("mae", 0) for t in trades) / n
            a_hold = sum(t.get("hold_days", 0) for t in trades) / n
            console.print(
                f"{et:16s} {n:5d} {wr:6.0%} {avg:+7.2f}% {a_win:+7.2f}% "
                f"{a_loss:7.2f}% {sl_cnt/n:6.0%} {a_mfe:+7.2f}% {a_mae:+7.2f}% {a_hold:6.1f}일"
            )

        # ── 손절 MFE 분포 ─────────────────────────────────────────────
        sl_trades = [t for t in all_trades if "손절" in t.get("reason", "") and "본전" not in t.get("reason", "")]
        if sl_trades:
            console.print(f"\n[bold]🔍 손절 MFE 분포[/bold] (총 {len(sl_trades)}건 | 진입 문제 vs 청산 문제 진단)")
            brackets = [
                ("진입 직후 하락 (MFE < 0%)",  lambda t: t.get("mfe", 0) < 0),
                ("MFE 0~1%  (진입 문제)",      lambda t: 0 <= t.get("mfe", 0) < 1),
                ("MFE 1~3%  (진입 약함)",       lambda t: 1 <= t.get("mfe", 0) < 3),
                ("MFE 3~5%  (청산 문제 의심)", lambda t: 3 <= t.get("mfe", 0) < 5),
                ("MFE 5%+   (청산 실패)",      lambda t: t.get("mfe", 0) >= 5),
            ]
            for label, fn in brackets:
                cnt = sum(1 for t in sl_trades if fn(t))
                pct = cnt / len(sl_trades)
                bar = "█" * int(pct * 24)
                console.print(f"  {label:30s} {cnt:4d} ({pct:.0%}) {bar}")

    # ── 등급 × 진입유형 교차표 ───────────────────────────────────────────────
    if all_trades:
        console.print("\n[bold]📐 등급 × 진입유형 교차표[/bold]")
        console.print(f"{'등급-유형':22s} {'거래':>5s} {'승률':>6s} {'평균(net)':>10s} {'손절률':>7s}")
        console.print("-" * 56)
        for g in ["A", "B", "C"]:
            gt = [t for t in all_trades if t.get("grade") == g]
            if not gt:
                continue
            for et in _ENTRY_TYPES:
                cross = [t for t in gt if t.get("entry_type") == et]
                if len(cross) < 3:
                    continue
                n   = len(cross)
                wr  = sum(1 for t in cross if t["pnl"] > 0) / n
                avg = sum(t["pnl"] for t in cross) / n
                slp = sum(1 for t in cross
                          if "손절" in t.get("reason", "") and "본전" not in t.get("reason", "")) / n
                console.print(f"{g}-{et:16s} {n:5d} {wr:6.0%} {avg:+10.2f}% {slp:7.0%}")
            console.print("")

    # ── 상위/하위 종목 ───────────────────────────────────────────────────
    if tradeable:
        console.print("\n[bold]🏆 수익률 상위 5종목[/bold]")
        for r in sorted(tradeable, key=lambda x: x.total_pnl, reverse=True)[:5]:
            console.print(f"  {r.symbol:6s} | {r.n}거래 | 승률 {r.win_rate:.0%} | 누적 {r.total_pnl:+.1f}%")
        console.print("\n[bold]📉 손실 하위 5종목[/bold]")
        for r in sorted(tradeable, key=lambda x: x.total_pnl)[:5]:
            console.print(f"  {r.symbol:6s} | {r.n}거래 | 승률 {r.win_rate:.0%} | 누적 {r.total_pnl:+.1f}%")

    # ── 월별 성과 ──────────────────────────────────────────────────────────────
    if all_trades:
        monthly: dict = {}
        for t in all_trades:
            ym = pd.Timestamp(t["exit"]).strftime("%Y-%m")
            monthly.setdefault(ym, []).append(t)
        console.print("\n[bold]📅 월별 성과 (청산 기준)[/bold]")
        console.print(f"  {'월':7s} {'거래':>5s} {'승률':>6s} {'평균net':>8s}")
        console.print("  " + "-" * 32)
        for ym in sorted(monthly):
            ms = monthly[ym]
            n  = len(ms)
            wr = sum(1 for t in ms if t["pnl"] > 0) / n
            avg = sum(t["pnl"] for t in ms) / n
            marker = "▲" if avg >= 0 else "▼"
            console.print(f"  {ym}  {n:4d} {wr:6.0%} {avg:+8.2f}%  {marker}")

    # ── 수익 집중도 ──────────────────────────────────────────────────────────
    if tradeable:
        sym_pnl = sorted(((r.symbol, r.total_pnl) for r in tradeable),
                         key=lambda x: x[1], reverse=True)
        total_gain = sum(p for _, p in sym_pnl if p > 0)
        total_loss = sum(p for _, p in sym_pnl if p < 0)
        console.print(f"\n[bold]🎯 수익 집중도[/bold]")
        console.print(f"  이익 합산 {total_gain:+.1f}% | 손실 합산 {total_loss:+.1f}%")
        cum = 0.0
        milestones = {3, 5, 10, 15, 20}
        for i, (sym, pnl) in enumerate(sym_pnl):
            if pnl <= 0:
                break
            cum += pnl
            if (i + 1) in milestones and total_gain > 0:
                console.print(f"  상위 {i+1:2d}종목 누적 {cum:+.1f}% ({cum/total_gain*100:.0f}% of 이익)")
        console.print(f"  반복손실 하위 10:")
        for sym, pnl in sym_pnl[-10:]:
            if pnl < 0:
                console.print(f"    {sym} {pnl:+.1f}%")

    # ── 포트폴리오 시뮬레이션 ──────────────────────────────────────────────────
    if all_trades:
        _compare_portfolio_scenarios(all_trades)

    if no_trade:
        console.print(f"\n  거래 없음 종목 ({len(no_trade)}개): {[r.symbol for r in no_trade]}")


# ── 엔트리포인트 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KospiBot v3 백테스트")
    parser.add_argument("--days",     type=int,   default=180)
    parser.add_argument("--top",      type=int,   default=0)
    parser.add_argument("--syms",     nargs="*")
    parser.add_argument("--universe", type=str,   default="")
    parser.add_argument("--stop",     type=float, default=-3.0)
    parser.add_argument("--trailing", type=float, default=5.0)
    parser.add_argument("--partial",  type=float, default=8.0)
    parser.add_argument("--turnover", type=float, default=30)
    parser.add_argument("--cost",     type=float, default=0.40,
                        help="왕복 거래비용 %% (수수료+세금+슬리피지, 기본 0.40)")
    args = parser.parse_args()

    if args.syms:
        symbols = args.syms
    elif args.universe == "mixed":
        try:
            kospi_df  = fdr.StockListing("KOSPI").sort_values("Marcap", ascending=False)
            kosdaq_df = fdr.StockListing("KOSDAQ").sort_values("Marcap", ascending=False)
            col_k = "Code" if "Code" in kospi_df.columns  else "Symbol"
            col_q = "Code" if "Code" in kosdaq_df.columns else "Symbol"
            kospi_syms  = kospi_df[col_k].tolist()[10:210]
            kosdaq_syms = kosdaq_df[col_q].tolist()[:50]
            kospi_set   = set(kospi_df[col_k].tolist())
            kosdaq_syms = [s for s in kosdaq_syms if s not in kospi_set]
            symbols     = kospi_syms + kosdaq_syms
            console.print(f"KOSPI 11~210위 {len(kospi_syms)}개 + KOSDAQ 상위 50 {len(kosdaq_syms)}개 = 총 {len(symbols)}개")
        except Exception as e:
            console.print(f"[red]종목 목록 실패: {e}[/red]")
            raise
    elif args.top > 0:
        try:
            df_list = fdr.StockListing("KOSPI").sort_values("Marcap", ascending=False)
            col = "Code" if "Code" in df_list.columns else "Symbol"
            symbols = df_list[col].tolist()[:args.top]
        except Exception as e:
            symbols = ["005930", "000660", "035420", "005380", "068270",
                       "000270", "005490", "051910", "006400", "034220"]
    else:
        symbols = [
            "005930", "000660", "035420", "005380", "068270",
            "000270", "005490", "051910", "006400", "034220",
            "035720", "066570", "028260", "003550", "012330",
            "096770", "010950", "009150", "018260", "011200",
        ]

    run(
        symbols,
        days=args.days,
        stop_loss_pct=args.stop,
        trailing_gap_pct=args.trailing,
        partial_profit_pct=args.partial,
        min_turnover=args.turnover * 1e8,
        cost_pct=args.cost,
    )
