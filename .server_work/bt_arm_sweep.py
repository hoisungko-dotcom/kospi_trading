#!/usr/bin/env python3
"""
박스봇 TRAILING_ARM_PCT 백테스트 (서버 실제 코드 사용)

종목   : KR_BOX_BOT_UNIVERSE_KOSPI 65개
데이터 : 키움 1분봉 get_min_chart (max_pages=10 → ~15 거래일)
스윕   : ARM_SWEEP (GAP 0.018 고정)
"""
from __future__ import annotations

import sys
import os
import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, "/home/ubuntu/kospi_box_bot")

from dotenv import load_dotenv
load_dotenv("/home/ubuntu/kospi_box_bot/.env")

# 일봉 API는 백테스트에서 스킵 (시간 절약)
os.environ["BOX_DAILY_ENABLED"] = "false"

from collector.kiwoom_client import get_min_chart, parse_candle
from collector.surge_detector import Candle
from realtime.box_checker import BoxChecker

logging.basicConfig(level=logging.WARNING, format="%(message)s")

# ── 설정 ─────────────────────────────────────────────────────────────────────
ARM_SWEEP  = [0.003, 0.005, 0.010, 0.015, 0.020]
GAP_PCT    = 0.018      # 서버 .env 고정값
STOP_LOSS  = -0.02      # -2% 손절
MAX_PAGES  = 10         # 1 page ≈ 900봉 ≈ 1.5 거래일
PER_TRADE  = 5_000_000  # 5백만원 / 포지션
CACHE_DIR  = Path("/home/ubuntu/kospi_box_bot/data/bt_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE = [
    "005930","000660","005380","000270","068270","035420","005490",
    "042660","066570","003550","012330","012450","267250","028260",
    "032830","017670","009540","078930","015760","055550","024110",
    "006800","071050","016360","005830","047810","272210","064350",
    "443060","064400","079550","042700","357780","307950","007660",
    "010120","018260","141080","214450","214150","145020",
    "237690","003230","030200","000720","028050","021240","267260",
    "277810","034730","010130","058470","039030","064760","034020",
    "032640","241560","000990","005290","058610","319660",
    "003670","247540","005935",
]


# ── 데이터 다운로드 ───────────────────────────────────────────────────────────
def fetch_candles(stk_cd: str) -> list[Candle]:
    cache = CACHE_DIR / f"{stk_cd}.json"
    if cache.exists():
        rows = json.loads(cache.read_text())
        return [Candle(**r) for r in rows]

    print(f"  다운로드: {stk_cd}", end="", flush=True)
    try:
        rows = get_min_chart(stk_cd, tic_scope="1", max_pages=MAX_PAGES)
        time.sleep(0.35)
    except Exception as e:
        print(f" ERR:{e}")
        return []

    if not rows:
        print(" (데이터없음)")
        return []

    candles: list[Candle] = []
    for row in rows:
        try:
            d = parse_candle(row)
            if d and d["close"] > 0 and d["ts"]:
                candles.append(Candle(
                    ts=d["ts"], open=d["open"], high=d["high"],
                    low=d["low"], close=d["close"], volume=d["volume"],
                ))
        except Exception:
            pass

    candles.sort(key=lambda c: c.ts)
    print(f" ({len(candles)}봉)")

    cache.write_text(json.dumps([
        {"ts": c.ts, "open": c.open, "high": c.high,
         "low": c.low, "close": c.close, "volume": c.volume}
        for c in candles
    ], ensure_ascii=False))
    return candles


# ── 단일 종목 시뮬레이션 ──────────────────────────────────────────────────────
@dataclass
class _Pos:
    entry_ts: str
    entry_price: float
    qty: int
    peak: float


def simulate_stock(candles: list[Candle], checker: BoxChecker,
                   arm_pct: float) -> list[dict]:
    """단일 종목 1포지션 시뮬레이션. 매수→보유→청산 반복."""
    trades: list[dict] = []
    pos: _Pos | None = None
    WINDOW = checker.max_length + 2

    for i in range(WINDOW, len(candles)):
        cur = candles[i]
        hhmm = cur.ts[8:12] if len(cur.ts) >= 12 else "0000"

        # 장 마감 강제 청산
        if hhmm >= "1518" and pos:
            pnl = (cur.close - pos.entry_price) / pos.entry_price
            trades.append({
                "entry_ts": pos.entry_ts, "exit_ts": cur.ts,
                "pnl_pct": round(pnl * 100, 3),
                "pnl_krw": int(pos.qty * (cur.close - pos.entry_price)),
                "reason": "eod_force",
            })
            pos = None
            continue

        # 거래 시간 외 스킵 (신규 매수만)
        if hhmm < "0905" or hhmm >= "1500":
            if pos:
                # 포지션은 계속 관리
                if cur.close > pos.peak:
                    pos.peak = cur.close
            continue

        if pos:
            # 포지션 관리: 고점 갱신
            if cur.close > pos.peak:
                pos.peak = cur.close

            pnl = (cur.close - pos.entry_price) / pos.entry_price
            peak_gain = (pos.peak - pos.entry_price) / pos.entry_price

            if pnl <= STOP_LOSS:
                trades.append({
                    "entry_ts": pos.entry_ts, "exit_ts": cur.ts,
                    "pnl_pct": round(pnl * 100, 3),
                    "pnl_krw": int(pos.qty * (cur.close - pos.entry_price)),
                    "reason": "stop_loss",
                })
                pos = None

            elif peak_gain >= arm_pct:
                floor = pos.peak * (1.0 - GAP_PCT)
                if cur.close <= floor:
                    trades.append({
                        "entry_ts": pos.entry_ts, "exit_ts": cur.ts,
                        "pnl_pct": round(pnl * 100, 3),
                        "pnl_krw": int(pos.qty * (cur.close - pos.entry_price)),
                        "reason": "trailing_stop",
                    })
                    pos = None

        else:
            # 신규 진입 신호 탐지
            window = candles[i - WINDOW: i + 1]
            ok, _ = checker.check(window, stk_cd="")
            if ok:
                entry_price = cur.close
                qty = int(PER_TRADE // entry_price)
                if qty > 0:
                    pos = _Pos(
                        entry_ts=cur.ts,
                        entry_price=entry_price,
                        qty=qty,
                        peak=entry_price,
                    )

    # 데이터 끝에서 미청산 포지션 청산
    if pos and candles:
        cur = candles[-1]
        pnl = (cur.close - pos.entry_price) / pos.entry_price
        trades.append({
            "entry_ts": pos.entry_ts, "exit_ts": cur.ts,
            "pnl_pct": round(pnl * 100, 3),
            "pnl_krw": int(pos.qty * (cur.close - pos.entry_price)),
            "reason": "end_of_data",
        })

    return trades


# ── 결과 출력 ─────────────────────────────────────────────────────────────────
def print_stats(arm: float, all_trades: list[dict]) -> None:
    if not all_trades:
        print(f"  ARM {arm*100:.1f}%: 거래없음")
        return

    wins  = [t for t in all_trades if t["pnl_krw"] > 0]
    loss  = [t for t in all_trades if t["pnl_krw"] <= 0]
    total = len(all_trades)
    wr    = len(wins) / total * 100

    avg_win  = sum(t["pnl_pct"] for t in wins)  / len(wins)  if wins  else 0.0
    avg_loss = sum(t["pnl_pct"] for t in loss)  / len(loss)  if loss  else 0.0
    ev       = sum(t["pnl_pct"] for t in all_trades) / total
    net_krw  = sum(t["pnl_krw"] for t in all_trades)

    by_reason: dict[str, int] = {}
    for t in all_trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1

    rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    print(
        f"  ARM {arm*100:.1f}%  |  "
        f"거래 {total:3d}건  |  승률 {wr:5.1f}%  |  "
        f"평균이익 {avg_win:+.3f}%  |  평균손실 {avg_loss:+.3f}%  |  "
        f"R:R {rr:.2f}  |  EV {ev:+.4f}%  |  "
        f"순손익 {net_krw:+,}원  |  사유: {by_reason}"
    )


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print(f"박스봇 TRAILING_ARM_PCT 스윕 백테스트")
    print(f"  종목 {len(UNIVERSE)}개  |  데이터 {MAX_PAGES}페이지/종목  |  GAP {GAP_PCT*100:.1f}%")
    print("=" * 70)

    # 1. 데이터 수집 (캐시 우선)
    print("\n[1단계] 1분봉 데이터 수집...")
    all_candles: dict[str, list[Candle]] = {}
    for stk_cd in UNIVERSE:
        c = fetch_candles(stk_cd)
        if len(c) >= 50:
            all_candles[stk_cd] = c

    print(f"\n  데이터 확보: {len(all_candles)}/{len(UNIVERSE)}개 종목")

    # 2. BoxChecker 인스턴스 (일봉 필터 OFF)
    checker = BoxChecker()

    # 3. ARM 값별 시뮬레이션
    print(f"\n[2단계] ARM 스윕 시뮬레이션 (GAP {GAP_PCT*100:.1f}% 고정)")
    print("-" * 70)

    for arm in ARM_SWEEP:
        all_trades: list[dict] = []
        for stk_cd, candles in all_candles.items():
            trades = simulate_stock(candles, checker, arm_pct=arm)
            for t in trades:
                t["stk_cd"] = stk_cd
            all_trades.extend(trades)
        print_stats(arm, all_trades)

    # 4. 현재 실행 중인 설정 강조
    print("-" * 70)
    print(f"현재 서버 설정: ARM 0.3%, GAP {GAP_PCT*100:.1f}%")
    print("=" * 70)


if __name__ == "__main__":
    main()
