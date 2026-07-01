"""아침 스크리닝 — 코스피/코스닥 전체 스캔 후 매수후보 선정

선정 기준: score 상위 (코스피 6개 + 코스닥 4개 기본값)
잡주 필터: 거래대금 30억 미만 제외, 우선주(끝자리 5) 보통주 존재 시 제외
"""
from __future__ import annotations

import logging
import os

import FinanceDataReader as fdr

from core.signal import SignalEngine

logger = logging.getLogger(__name__)

KOSPI_COUNT  = int(os.getenv("KOSPI_COUNT",  "6"))
KOSDAQ_COUNT = int(os.getenv("KOSDAQ_COUNT", "4"))
KOSDAQ_TOP_N = int(os.getenv("KOSDAQ_TOP_N", "300"))
MIN_TURNOVER_SCREEN = float(os.getenv("MIN_TURNOVER_SCREEN", "3000000000"))  # 30억


class Screener:
    def __init__(self, broker_client, async_client):
        self.broker_client = broker_client
        self.async_client = async_client
        self.engine = SignalEngine()

    def run(
        self,
        existing_holdings: set[str],
    ) -> tuple[list[str], set[str], dict[str, dict]]:
        """
        코스피/코스닥 스캔 → 매수후보 선정.

        Returns:
            candidates : 매수후보 (KOSPI + KOSDAQ)
            kospi_set  : 코스피 종목 집합 (시장 구분용)
            price_map  : {symbol: price_data}
        """
        kospi_syms  = self._get_kospi_symbols()
        kosdaq_syms = self._get_kosdaq_symbols()
        kospi_set   = set(kospi_syms)
        kosdaq_syms = [s for s in kosdaq_syms if s not in kospi_set]
        all_syms    = kospi_syms + kosdaq_syms

        logger.info(
            "📊 코스피 %d개 + 코스닥 %d개 = 총 %d개 스캔",
            len(kospi_syms), len(kosdaq_syms), len(all_syms),
        )

        results   = self.async_client.fetch_all_stocks(all_syms, kospi_set=kospi_set)
        price_map: dict[str, dict] = {}
        kospi_scores:  dict[str, float] = {}
        kosdaq_scores: dict[str, float] = {}

        for r in results:
            if not r.get("data"):
                continue
            sym  = r["symbol"]
            data = r["data"]
            close      = float(data.get("close", 0) or 0)
            avg_volume = float(data.get("avg_volume_20", 0) or 0)
            # 잡주 필터: 거래대금 30억 미만
            if close > 0 and avg_volume > 0 and close * avg_volume < MIN_TURNOVER_SCREEN:
                continue
            sc = self.engine.score(sym, data)
            price_map[sym] = data
            if sym in kospi_set:
                kospi_scores[sym] = sc
            else:
                kosdaq_scores[sym] = sc

        top_kospi  = self._rank(kospi_scores,  existing_holdings, KOSPI_COUNT)
        top_kosdaq = self._rank(kosdaq_scores, existing_holdings, KOSDAQ_COUNT)
        candidates = top_kospi + top_kosdaq

        logger.info(
            "✅ 매수후보 — KOSPI %d개: %s | KOSDAQ %d개: %s",
            len(top_kospi), top_kospi, len(top_kosdaq), top_kosdaq,
        )
        return candidates, kospi_set, price_map

    def _rank(
        self,
        scores: dict[str, float],
        existing: set[str],
        n: int,
    ) -> list[str]:
        """점수 내림차순 + 우선주 중복 제거 + 기보유 종목 제외."""
        result    = []
        all_codes = set(scores)
        for sym, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            if sym in existing:
                continue
            # 우선주 제외 — 보통주(끝자리 0)가 목록에 있을 때
            if sym.endswith("5") and sym[:-1] + "0" in all_codes:
                continue
            result.append(sym)
            if len(result) >= n:
                break
        return result

    def _get_kospi_symbols(self) -> list[str]:
        try:
            df = fdr.StockListing("KOSPI")
            col = "Code" if "Code" in df.columns else "Symbol"
            return df[col].tolist()
        except Exception as e:
            logger.warning("코스피 종목 조회 실패: %s", e)
            return []

    def _get_kosdaq_symbols(self) -> list[str]:
        try:
            df = fdr.StockListing("KOSDAQ")
            col = "Code" if "Code" in df.columns else "Symbol"
            return df[col].tolist()[:KOSDAQ_TOP_N]
        except Exception as e:
            logger.warning("코스닥 종목 조회 실패: %s", e)
            return []
