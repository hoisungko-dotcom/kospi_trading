"""업종 모멘텀 모니터 — KIS 업종 분봉 API 기반."""
import json
import logging
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# KIS inquire-price output.bstp_kor_isnm → 섹터 코드 매핑
KIS_NAME_TO_SECTOR: Dict[str, str] = {
    '음식료품': '1001', '식음료': '1001',
    '섬유·의복': '1002', '섬유의복': '1002',
    '종이·목재': '1003', '종이목재': '1003',
    '화학': '1004',
    '의약품': '1005', '제약': '1005',
    '비금속광물': '1006',
    '철강·금속': '1007', '철강금속': '1007', '철강': '1007',
    '기계': '1008',
    '전기·전자': '1009', '전기전자': '1009',
    '의료·정밀기기': '1010', '의료정밀': '1010',
    '운송장비·부품': '1011', '운수장비': '1011', '자동차': '1011',
    '유통': '1012', '유통업': '1012',
    '전기·가스': '1013', '전기가스': '1013',
    '건설': '1014', '건설업': '1014',
    '운수·창고': '1015', '운수창고': '1015',
    '통신': '1016', '통신업': '1016',
    '금융': '1017', '금융업': '1017',
    '은행': '1018',
    '증권': '1019',
    '보험': '1020',
    '서비스업': '1021', '서비스': '1021',
    'IT 서비스': '2005', 'IT서비스': '2005', '소프트웨어': '2005',
    '반도체': '2005',
}

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

        # 종목→섹터 매핑은 백그라운드에서 빌드 (KIS API 조회)
        t = threading.Thread(target=self._build_stock_sector_map, daemon=True)
        t.start()

    # ── 종목→섹터 매핑 ────────────────────────────────────────────────────

    def _lookup_sector_via_kis(self, symbol: str) -> str | None:
        """KIS inquire-price API로 종목 단건 조회 → 섹터 코드 반환 (없으면 None)."""
        try:
            url = (
                f"{self._client.data_base_url}"
                "/uapi/domestic-stock/v1/quotations/inquire-price"
            )
            headers = {
                "content-type":  "application/json; charset=utf-8",
                "authorization": f"Bearer {self._client.data_token}",
                "appkey":        self._client.data_appkey,
                "appsecret":     self._client.data_appsecret,
                "tr_id":         "FHKST01010100",
                "custtype":      "P",
            }
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD":         symbol,
            }
            res = self._client.session.get(
                url, headers=headers, params=params, timeout=5
            )
            if res.status_code != 200:
                return None
            data = res.json()
            if data.get("rt_cd") != "0":
                return None
            bstp_name = (data.get("output") or {}).get("bstp_kor_isnm", "")
            return KIS_NAME_TO_SECTOR.get(bstp_name)
        except Exception:
            return None

    def _build_stock_sector_map(self):
        """심볼 파일(kospi_symbols.json, kosdaq_symbols.json, top_10_daily.json) 기반으로
        KIS inquire-price API를 배치 조회해 종목→섹터 코드 매핑을 구성한다.

        파일 우선순위:
          1. data/kospi_symbols.json   — 전체 코스피 심볼 (시스템이 생성한 경우)
          2. data/kosdaq_symbols.json  — 전체 코스닥 심볼 (시스템이 생성한 경우)
          3. data/top_10_daily.json    — 오늘 매수후보 10종목 (위 두 파일 없을 때 폴백)
        """
        try:
            data_dir = Path(__file__).parent.parent / "data"
            symbols: list[str] = []

            # 1·2 전체 심볼 파일 시도
            for fname in ("kospi_symbols.json", "kosdaq_symbols.json"):
                fpath = data_dir / fname
                if fpath.exists():
                    try:
                        raw = json.loads(fpath.read_text(encoding="utf-8"))
                        # 리스트 또는 {"symbols": [...]} / {"data": [...]} 형태 지원
                        if isinstance(raw, list):
                            symbols.extend(raw)
                        elif isinstance(raw, dict):
                            symbols.extend(raw.get("symbols", raw.get("data", [])))
                    except Exception as e:
                        logger.warning(f"⚠️ {fname} 읽기 실패: {e}")
                else:
                    logger.debug(f"[섹터] {fpath} 없음 — 건너뜀")

            # 3 폴백: top_10_daily.json
            if not symbols:
                fallback = data_dir / "top_10_daily.json"
                if fallback.exists():
                    try:
                        raw = json.loads(fallback.read_text(encoding="utf-8"))
                        symbols = raw.get("symbols", [])
                        logger.info(
                            f"[섹터] 전체 심볼 파일 없음 — top_10_daily.json "
                            f"폴백({len(symbols)}종목) 사용"
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ top_10_daily.json 읽기 실패: {e}")

            if not symbols:
                logger.warning(
                    "⚠️ 섹터 매핑용 심볼 파일 없음 — 빈 매핑으로 종료 "
                    "(lazy lookup으로 개별 조회는 계속 동작)"
                )
                self._map_ready = True
                return

            mapping: Dict[str, str] = {}
            for sym in symbols:
                sector_code = self._lookup_sector_via_kis(sym)
                if sector_code:
                    mapping[sym] = sector_code
                time.sleep(0.06)   # KIS API rate-limit 준수

            self._stock_sector = mapping
            self._map_ready    = True
            logger.info(
                f"✅ 섹터 매핑 완료 (KIS API): {len(mapping)}개 종목 / "
                f"총 {len(symbols)}개 조회 → {len(ALL_SECTORS)}개 업종"
            )
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
        """종목의 섹터 모멘텀 보너스 (-0.15 ~ +0.15). 매핑 없으면 KIS 단건 조회 후 캐시."""
        sector = self._stock_sector.get(symbol)
        if not sector:
            # lazy lookup: 아직 매핑되지 않은 종목을 즉석 조회
            sector = self._lookup_sector_via_kis(symbol)
            if sector:
                self._stock_sector[symbol] = sector
                logger.debug(f"[섹터] {symbol} lazy 매핑 → {ALL_SECTORS.get(sector, sector)}")
            else:
                return 0.0
        return self._sector_momentum.get(sector, 0.0)

    def get_sector_name(self, symbol: str) -> str:
        return ALL_SECTORS.get(self._stock_sector.get(symbol, ""), "")
