"""
과거 급등 패턴 수집기
사용법: python -m collector.batch_collector --market 0 --pages 5

1. KOSPI/KOSDAQ 전체 종목 조회
2. 각 종목의 1분봉 900×pages건 수집
3. SurgeDetector로 급등 이벤트 탐지
4. PatternStore에 저장
"""
from __future__ import annotations

import argparse
import logging
import time
from collector.kiwoom_client import get_stock_list, get_min_chart, parse_candle
from collector.surge_detector import Candle, SurgeDetector
from analysis.pattern_store import PatternStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="0", help="0=KOSPI, 10=KOSDAQ")
    ap.add_argument("--pages", type=int, default=3, help="종목당 연속 조회 페이지수 (1페이지=900봉)")
    ap.add_argument("--surge-pct", type=float, default=0.03)
    ap.add_argument("--vol-mult",  type=float, default=3.0)
    ap.add_argument("--lookback",  type=int,   default=5)
    ap.add_argument("--delay",     type=float, default=0.3, help="종목간 API 딜레이(초)")
    args = ap.parse_args()

    store    = PatternStore()
    detector = SurgeDetector(
        surge_pct=args.surge_pct,
        vol_mult=args.vol_mult,
        lookback=args.lookback,
    )

    logger.info("종목 목록 조회 중 (market=%s)...", args.market)
    stock_list = get_stock_list(args.market)
    logger.info("총 %d 종목", len(stock_list))

    total_events = 0
    for idx, stk in enumerate(stock_list):
        stk_cd = stk.get("code") or stk.get("stk_cd") or stk.get("stk_code", "")
        if not stk_cd:
            continue

        try:
            rows = get_min_chart(stk_cd, tic_scope="1", max_pages=args.pages)
        except Exception as e:
            logger.warning("[%s] 조회 실패: %s", stk_cd, e)
            time.sleep(args.delay)
            continue

        if not rows:
            time.sleep(args.delay)
            continue

        # API 응답은 최신 → 오래된 순 → 역전
        candles: list[Candle] = [Candle(**parse_candle(r)) for r in reversed(rows)]

        events = 0
        for i in range(len(candles)):
            ev = detector.check(stk_cd, candles[: i + 1])
            if ev:
                store.add(ev)
                events += 1
                total_events += 1

        if events:
            logger.info("[%d/%d] %s: 급등 %d건 저장", idx + 1, len(stock_list), stk_cd, events)

        time.sleep(args.delay)

    logger.info("수집 완료. 총 급등 이벤트 %d건 / 저장소 %d건", total_events, store.count)


if __name__ == "__main__":
    main()
