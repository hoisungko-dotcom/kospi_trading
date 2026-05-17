"""
국내 주식 데이터 수집 — KIS 실전 기간별시세 우선 방식.
Yahoo Finance는 국내 종목 누락이 잦아 스크리닝 주 데이터 소스로 사용하지 않는다.
"""
import logging
import time
from typing import List, Dict

logger = logging.getLogger(__name__)


class AsyncDataClientKospi:
    """국내 주식 데이터 수집 클라이언트 (KIS 실전 시세 우선)"""

    def __init__(self, kis_client, **kwargs):
        self.kis_client = kis_client

    def fetch_all_stocks(self, symbols: List[str], kospi_set: set = None) -> List[Dict]:
        """KIS 실전 기간별시세로 전 종목 수집."""
        if not symbols:
            return []

        logger.info(f"🔄 {len(symbols)}개 종목 KIS 일봉 수집 중...")
        t0 = time.time()

        results = self.kis_client.get_bulk_daily_ohlcv(symbols, kospi_set=kospi_set)

        success = sum(1 for r in results if r['data'] is not None)
        logger.info(
            f"✅ 수집 완료: {time.time() - t0:.1f}초 "
            f"({success}/{len(symbols)}개 성공)"
        )
        return results
