#!/usr/bin/env python3
"""
박스봇 신규 설정 백테스트 (캐시 재사용)

비교:
  A) 구설정: ARM 0.3%, GAP 1.8%, follow_through OFF, time_stop OFF
  B) 신설정: ARM 0.3%, GAP 1.0%, follow_through 3봉/0.2%, time_stop 30봉
  C) 변형 1: B + ARM 0.5%
  D) 변형 2: B + follow_through 2봉/0.15%  (더 빠른 청산)
"""
from __future__ import annotations

import sys, os, json, logging
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, "/home/ubuntu/kospi_box_bot")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/kospi_box_bot/.env")
os.environ["BOX_DAILY_ENABLED"] = "false"

from collector.surge_detector import Candle
from realtime.box_checker import BoxChecker

logging.basicConfig(level=logging.WARNING, format="%(message)s")

CACHE_DIR = Path("/home/ubuntu/kospi_box_bot/data/bt_cache")
STOP_LOSS = -0.02
PER_TRADE = 5_000_000

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

CONFIGS = {
    "A_구설정(ARM0.3%,GAP1.8%,FT×,TS×)": dict(
        arm=0.003, gap=0.018,
        ft_enabled=False, ft_bars=3, ft_min_gain=0.002,
        ts_enabled=False, ts_bars=30,
    ),
    "B_신설정(ARM0.3%,GAP1.0%,FT3봉,TS30봉)": dict(
        arm=0.003, gap=0.010,
        ft_enabled=True, ft_bars=3, ft_min_gain=0.002,
        ts_enabled=True, ts_bars=30,
    ),
    "C_변형(ARM0.5%,GAP1.0%,FT3봉,TS30봉)": dict(
        arm=0.005, gap=0.010,
        ft_enabled=True, ft_bars=3, ft_min_gain=0.002,
        ts_enabled=True, ts_bars=30,
    ),
    "D_변형(ARM0.3%,GAP1.0%,FT2봉,TS20봉)": dict(
        arm=0.003, gap=0.010,
        ft_enabled=True, ft_bars=2, ft_min_gain=0.0015,
        ts_enabled=True, ts_bars=20,
    ),
}


def load_cache() -> dict[str, list[Candle]]:
    all_candles = {}
    for stk_cd in UNIVERSE:
        cache = CACHE_DIR / f"{stk_cd}.json"
        if cache.exists():
            rows = json.loads(cache.read_text())
            c = [Candle(**r) for r in rows]
            if len(c) >= 50:
                all_candles[stk_cd] = c
    return all_candles


@dataclass
class _Pos:
    entry_ts: str
    entry_price: float
    qty: int
    peak: float
    box_high: float
    candles_held: int = 0


def simulate_stock(candles: list[Candle], checker: BoxChecker, cfg: dict) -> list[dict]:
    arm = cfg["arm"]
    gap = cfg["gap"]
    ft_enabled = cfg["ft_enabled"]
    ft_bars = cfg["ft_bars"]
    ft_min_gain = cfg["ft_min_gain"]
    ts_enabled = cfg["ts_enabled"]
    ts_bars = cfg["ts_bars"]

    trades: list[dict] = []
    pos: _Pos | None = None
    WINDOW = checker.max_length + 2

    for i in range(WINDOW, len(candles)):
        cur = candles[i]
        hhmm = cur.ts[8:12] if len(cur.ts) >= 12 else "0000"

        # EOD 강제 청산
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

        if hhmm < "0905":
            continue

        if pos:
            pos.candles_held += 1
            if cur.close > pos.peak:
                pos.peak = cur.close

            pnl = (cur.close - pos.entry_price) / pos.entry_price
            peak_gain = (pos.peak - pos.entry_price) / pos.entry_price

            # 1) 손절
            if pnl <= STOP_LOSS:
                trades.append({"entry_ts": pos.entry_ts, "exit_ts": cur.ts,
                    "pnl_pct": round(pnl*100,3),
                    "pnl_krw": int(pos.qty*(cur.close-pos.entry_price)),
                    "reason": "stop_loss"})
                pos = None; continue

            # 2) Follow-through 실패 조기 청산
            if ft_enabled:
                if pos.candles_held >= 2 and pos.box_high > 0 and cur.close < pos.box_high:
                    trades.append({"entry_ts": pos.entry_ts, "exit_ts": cur.ts,
                        "pnl_pct": round(pnl*100,3),
                        "pnl_krw": int(pos.qty*(cur.close-pos.entry_price)),
                        "reason": "follow_through_fail"})
                    pos = None; continue
                if pos.candles_held >= ft_bars and peak_gain < ft_min_gain:
                    trades.append({"entry_ts": pos.entry_ts, "exit_ts": cur.ts,
                        "pnl_pct": round(pnl*100,3),
                        "pnl_krw": int(pos.qty*(cur.close-pos.entry_price)),
                        "reason": "follow_through_fail"})
                    pos = None; continue

            # 3) 타임스톱
            if ts_enabled and pos.candles_held >= ts_bars and peak_gain < arm:
                trades.append({"entry_ts": pos.entry_ts, "exit_ts": cur.ts,
                    "pnl_pct": round(pnl*100,3),
                    "pnl_krw": int(pos.qty*(cur.close-pos.entry_price)),
                    "reason": "time_stop"})
                pos = None; continue

            # 4) 트레일링 스톱
            if peak_gain >= arm:
                floor = pos.peak * (1.0 - gap)
                if cur.close <= floor:
                    trades.append({"entry_ts": pos.entry_ts, "exit_ts": cur.ts,
                        "pnl_pct": round(pnl*100,3),
                        "pnl_krw": int(pos.qty*(cur.close-pos.entry_price)),
                        "reason": "trailing_stop"})
                    pos = None; continue

            # 15:00 이후 신규 진입 없음 (포지션은 유지)
        elif hhmm < "1500":
            window = candles[i - WINDOW: i + 1]
            ok, info = checker.check(window, stk_cd="")
            if ok:
                qty = int(PER_TRADE // cur.close)
                if qty > 0:
                    pos = _Pos(entry_ts=cur.ts, entry_price=cur.close, qty=qty,
                               peak=cur.close, box_high=info.get("box_high", 0.0))

    # 미청산 포지션
    if pos and candles:
        cur = candles[-1]
        pnl = (cur.close - pos.entry_price) / pos.entry_price
        trades.append({"entry_ts": pos.entry_ts, "exit_ts": cur.ts,
            "pnl_pct": round(pnl*100,3),
            "pnl_krw": int(pos.qty*(cur.close-pos.entry_price)),
            "reason": "end_of_data"})
    return trades


def print_stats(label: str, trades: list[dict]) -> None:
    if not trades:
        print(f"  {label}: 거래없음")
        return

    wins  = [t for t in trades if t["pnl_krw"] > 0]
    loss  = [t for t in trades if t["pnl_krw"] <= 0]
    total = len(trades)
    wr    = len(wins) / total * 100
    avg_w = sum(t["pnl_pct"] for t in wins) / len(wins)  if wins else 0
    avg_l = sum(t["pnl_pct"] for t in loss) / len(loss)  if loss else 0
    ev    = sum(t["pnl_pct"] for t in trades) / total
    net   = sum(t["pnl_krw"] for t in trades)
    rr    = abs(avg_w / avg_l) if avg_l != 0 else 0

    by_r: dict[str, int] = {}
    for t in trades:
        by_r[t["reason"]] = by_r.get(t["reason"], 0) + 1

    print(f"\n  [{label}]")
    print(f"  거래 {total}건 | 승률 {wr:.1f}% | 평균이익 {avg_w:+.3f}% | 평균손실 {avg_l:+.3f}% | R:R {rr:.2f} | EV {ev:+.4f}% | 순손익 {net:+,}원")
    print(f"  청산사유: {by_r}")


def main():
    print("=" * 72)
    print("박스봇 신구 설정 비교 백테스트 (캐시 사용)")
    print("=" * 72)

    all_candles = load_cache()
    print(f"캐시 로드: {len(all_candles)}개 종목\n")

    checker = BoxChecker()

    for label, cfg in CONFIGS.items():
        all_trades: list[dict] = []
        for candles in all_candles.values():
            all_trades.extend(simulate_stock(candles, checker, cfg))
        print_stats(label, all_trades)

    print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
