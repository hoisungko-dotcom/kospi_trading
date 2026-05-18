"""업종 모멘텀 모니터 — KIS 업종 분봉 API 기반."""
import logging
import time
import threading
from datetime import datetime
from typing import Dict
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# KIS 업종 분봉 API 코드 → 업종명
KOSPI_SECTORS: Dict[str, str] = {
    "1001": "음식료품",  "1002": "섬유의복",   "1003": "종이목재",
    "1004": "화학",      "1005": "의약품",      "1006": "비금속광물",
    "1007": "철강금속",  "1008": "기계",        "1009": "전기전자",
    "1010": "의료정밀",  "1011": "운수장비",    "1012": "유통업",
    "1013": "전기가스",  "1014": "건설업",      "1015": "운수창고",
    "1016": "통신업",    "1017": "금융업",      "1018": "은행",
    "1019": "증권",      "1020": "보험",        "1021": "서비스업",
}
KOSDAQ_SECTORS: Dict[str, str] = {
    "2001": "KOSDAQ종합",
    "2005": "KOSDAQ_IT",
    "2006": "KOSDAQ제약",
    "2007": "KOSDAQ제조",
}
ALL_SECTORS = {**KOSPI_SECTORS, **KOSDAQ_SECTORS}


class SectorMonitor:
    """
    업종 모멘텀 점수 관리.
    - _build_stock_sector_map(): pykrx로 종목→업종 매핑 (백그라운드)
    - update(): KIS 업종 분봉 API → 섹터 모멘텀 갱신 (10분 간격)
    - get_sector_bonus(symbol): -0.15 ~ +0.15 반환
    """

    def __init__(self, api_client, update_interval_sec: int = 600):
        self._client          = api_client       # KISClient 인스턴스
        self._interval        = update_interval_sec
        self._last_update     = 0.0
        self._sector_momentum: Dict[str, float] = {}   # {sector_code: factor}
        self._stock_sector:    Dict[str, str]   = {}   # {symbol: sector_code}
        self._map_ready       = False

        # 종목→섹터 매핑은 백그라운드에서 빌드 (pykrx 호출이 느릴 수 있음)
        t = threading.Thread(target=self._build_stock_sector_map, daemon=True)
        t.start()

    # ── 종목→섹터 매핑 ────────────────────────────────────────────────────

    def _build_stock_sector_map(self):
        try:
            from pykrx import stock as pykrx_stock
            today = datetime.now(KST).strftime('%Y%m%d')
            mapping: Dict[str, str] = {}

            for code in list(KOSPI_SECTORS) + list(KOSDAQ_SECTORS):
                try:
                    holdings = pykrx_stock.get_index_portfolio_holdings(today, code)
                    for sym in (holdings or []):
                        if sym not in mapping:          # 먼저 걸린 섹터 우선
                            mapping[sym] = code
                    time.sleep(0.08)
                except Exception:
                    pass

            self._stock_sector = mapping
            self._map_ready    = True
            logger.info(f"✅ 섹터 매핑 완료: {len(mapping)}개 종목 → {len(ALL_SECTORS)}개 업종")
        except Exception as e:
            logger.warning(f"⚠️ 섹터 매핑 실패 (섹터 가중치 비활성화): {e}")
            self._map_ready = True      # 실패해도 ready=True로 블로킹 방지

    # ── 섹터 모멘텀 갱신 ─────────────────────────────────────────────────

    def update(self, force: bool = False):
        """10분 간격으로 업종 분봉 데이터를 갱신한다."""
        if not force and time.time() - self._last_update < self._interval:
            return

        updated = 0
        for code in ALL_SECTORS:
            factor = self._fetch_momentum(code)
            if factor is not None:
                self._sector_momentum[code] = factor
                updated += 1
            time.sleep(0.05)

        self._last_update = time.time()
        if updated:
            top3 = sorted(self._sector_momentum.items(), key=lambda x: x[1], reverse=True)[:3]
            top3_str = ", ".join(f"{ALL_SECTORS.get(c,c)} {v:+.3f}" for c, v in top3)
            logger.info(f"📊 섹터 모멘텀 갱신 ({updated}개) | 상위: {top3_str}")

    def _fetch_momentum(self, sector_code: str) -> float | None:
        """KIS 업종 분봉 5봉 기준 모멘텀 계산 → -0.15 ~ +0.15."""
        try:
            url = f"{self._client.data_base_url}/uapi/domestic-stock/v1/quotations/inquire-time-indexchartprice"
            headers = {
                "content-type":  "application/json; charset=utf-8",
                "authorization": f"Bearer {self._client.data_token}",
                "appkey":        self._client.data_appkey,
                "appsecret":     self._client.data_appsecret,
                "tr_id":         "FHKUP03500200",
                "custtype":      "P",
            }
            params = {
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_ETC_CLS_CODE":       "0",
                "FID_INPUT_ISCD":         sector_code,
                "FID_INPUT_HOUR_1":       "600",   # 10분봉
                "FID_PW_DATA_INCU_YN":    "N",     # 당일
            }
            res = self._client.session.get(url, headers=headers, params=params, timeout=5)
            if res.status_code != 200:
                return None
            data = res.json()
            if data.get("rt_cd") != "0":
                return None

            candles = data.get("output2") or []
            closes = []
            for c in candles[:5]:
                v = float(c.get("bstp_nmix_prpr") or 0)
                if v > 0:
                    closes.append(v)

            if len(closes) < 2:
                return None

            # 최신봉(closes[0]) vs 5봉 전(closes[-1]) 변화율
            momentum = (closes[0] - closes[-1]) / closes[-1]
            # ±1.5% 변동 → ±0.15 보정계수 (선형 스케일링)
            return max(-0.15, min(0.15, momentum * 10))
        except Exception:
            return None

    # ── 종목 보너스 조회 ─────────────────────────────────────────────────

    def get_sector_bonus(self, symbol: str) -> float:
        """종목의 섹터 모멘텀 보너스 (-0.15 ~ +0.15). 매핑 없으면 0."""
        sector = self._stock_sector.get(symbol)
        if not sector:
            return 0.0
        return self._sector_momentum.get(sector, 0.0)

    def get_sector_name(self, symbol: str) -> str:
        return ALL_SECTORS.get(self._stock_sector.get(symbol, ""), "")
