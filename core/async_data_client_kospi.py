"""
국내 주식 데이터 수집.

현재 활성 브로커의 market-data provider를 사용한다.
"""
import logging
import time
from typing import List, Dict

logger = logging.getLogger(__name__)


class AsyncDataClientKospi:
    """국내 주식 데이터 수집 클라이언트."""

    def __init__(self, market_data_provider, **kwargs):
        self.market_data_provider = market_data_provider

    def fetch_all_stocks(self, symbols: List[str], kospi_set: set = None) -> List[Dict]:
        """활성 provider로 전 종목 일봉 수집."""
        if not symbols:
            return []

        logger.info(f"🔄 {len(symbols)}개 종목 브로커 일봉 수집 중...")
        t0 = time.time()

        results = self.market_data_provider.get_bulk_daily_ohlcv(symbols, kospi_set=kospi_set)

        success = sum(1 for r in results if r['data'] is not None)
        logger.info(
            f"✅ 수집 완료: {time.time() - t0:.1f}초 "
            f"({success}/{len(symbols)}개 성공)"
        )
        return results
