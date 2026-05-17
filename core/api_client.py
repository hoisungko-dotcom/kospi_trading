import requests
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_CACHE_DIR = Path(__file__).parent.parent / "data"


class KISClient:
    """하이브리드 KIS API 클라이언트 (실전 시세 + 모의/실전 주문)"""

    def __init__(self):
        # 1. 시세 조회용 (무조건 실전 서버 사용)
        self.data_base_url  = "https://openapi.koreainvestment.com:9443"
        self.data_appkey    = os.getenv('KIS_REAL_APPKEY')    or os.getenv('KIS_APPKEY')
        self.data_appsecret = os.getenv('KIS_REAL_APPSECRET') or os.getenv('KIS_APPSECRET')
        self.data_token     = None

        # 2. 주문 실행용 (모의/실전 선택)
        self.is_mock = os.getenv("MOCK_TRADING", "true").lower() == "true"
        if self.is_mock:
            self.trade_base_url  = "https://openapivts.koreainvestment.com:29443"
            self.trade_appkey    = os.getenv('KIS_MOCK_APPKEY')    or os.getenv('KIS_APPKEY')
            self.trade_appsecret = os.getenv('KIS_MOCK_APPSECRET') or os.getenv('KIS_APPSECRET')
            self.trade_account   = os.getenv('KIS_MOCK_ACCOUNT')   or os.getenv('KIS_ACCOUNT')
        else:
            self.trade_base_url  = "https://openapi.koreainvestment.com:9443"
            self.trade_appkey    = os.getenv('KIS_REAL_APPKEY')    or os.getenv('KIS_APPKEY')
            self.trade_appsecret = os.getenv('KIS_REAL_APPSECRET') or os.getenv('KIS_APPSECRET')
            self.trade_account   = os.getenv('KIS_REAL_ACCOUNT')   or os.getenv('KIS_ACCOUNT')

        logger.info(
            f"KISClient 초기화 — {'모의' if self.is_mock else '실전'}투자 | "
            f"계좌: {self.trade_account or '❌ 미설정'} | "
            f"AppKey: {'✅' if self.trade_appkey else '❌ 미설정'}"
        )

        self.trade_token = None
        self.session = requests.Session()

    # ── 토큰 발급 (캐시 우선) ─────────────────────────────────────────────

    def _cache_path(self, key: str) -> Path:
        _TOKEN_CACHE_DIR.mkdir(exist_ok=True)
        safe = (key or "nokey")[-8:]
        return _TOKEN_CACHE_DIR / f".token_{safe}.json"

    def _load_cached_token(self, key: str) -> str | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text())
            issued = datetime.fromisoformat(obj['issued_at'])
            if (datetime.now() - issued).total_seconds() < 82800:  # 23시간
                return obj['token']
        except Exception:
            pass
        return None

    def _save_cached_token(self, key: str, token: str):
        try:
            self._cache_path(key).write_text(
                json.dumps({'token': token, 'issued_at': datetime.now().isoformat()}),
                encoding='utf-8'
            )
        except Exception:
            pass

    def _delete_cached_token(self, key: str):
        try:
            self._cache_path(key).unlink(missing_ok=True)
        except Exception:
            pass

    def _get_token(self, url, key, secret, force: bool = False):
        """토큰 발급: 캐시 우선 → 신규 발급 → 403 시 62초 후 재시도"""
        cached = None if force else self._load_cached_token(key)
        if cached:
            logger.info(f"✅ 캐시된 토큰 재사용 ({url})")
            return cached

        auth_url = f"{url}/oauth2/tokenP"
        headers  = {"content-type": "application/json"}
        data     = {"grant_type": "client_credentials", "appkey": key, "appsecret": secret}

        if not key or not secret:
            logger.error(f"❌ 토큰 발급 불가 — AppKey 또는 AppSecret 미설정 (url={url})")
            return None

        for attempt in range(2):
            try:
                if attempt > 0:
                    logger.info("⏳ 토큰 rate limit — 62초 후 재시도...")
                    time.sleep(62)
                else:
                    time.sleep(0.5)

                response = self.session.post(auth_url, headers=headers, data=json.dumps(data))
                if response.status_code == 200:
                    token = response.json().get("access_token")
                    if token:
                        logger.info(f"✅ 토큰 발급 성공 ({url})")
                        self._save_cached_token(key, token)
                        return token
                    logger.error(f"❌ 토큰 응답에 access_token 없음: {response.text[:200]}")
                    return None

                # rate limit → 재시도
                if response.status_code == 403 and 'EGW00133' in response.text:
                    logger.warning(f"⚠️ 토큰 rate limit (403) — attempt {attempt+1}/2")
                    continue

                logger.error(f"❌ 토큰 발급 실패 HTTP {response.status_code}: {response.text[:200]}")
                return None
            except Exception as e:
                logger.error(f"❌ 토큰 발급 오류: {e}")
                return None

        logger.error("❌ 토큰 발급 2회 실패")
        return None

    def refresh_trade_token(self, force: bool = True) -> str | None:
        """주문용 토큰을 강제로 재발급하고 현재 클라이언트에 반영."""
        if force:
            self._delete_cached_token(self.trade_appkey)
        token = self._get_token(
            self.trade_base_url,
            self.trade_appkey,
            self.trade_appsecret,
            force=force,
        )
        if token:
            self.trade_token = token
            logger.info("✅ 주문 토큰 갱신 완료")
        else:
            logger.error("❌ 주문 토큰 갱신 실패 — KIS 응답 없음 또는 오류")
        return token

    def proactive_refresh_tokens(self):
        """장 시작 전 선제적 토큰 전체 갱신 (data + trade). 스케줄러에서 호출."""
        logger.info("🔄 선제적 토큰 전체 갱신 시작...")
        self._delete_cached_token(self.data_appkey)
        self.data_token = self._get_token(
            self.data_base_url, self.data_appkey, self.data_appsecret, force=True
        )
        if self.is_mock and self.trade_appkey != self.data_appkey:
            time.sleep(1)
        self.trade_token = None
        new_trade = self.refresh_trade_token(force=True)
        logger.info(
            f"✅ 선제 갱신 완료 — data: {'OK' if self.data_token else 'FAIL'}, "
            f"trade: {'OK' if new_trade else 'FAIL'}"
        )

    def authenticate(self):
        """시세용 토큰과 주문용 토큰을 각각 발급"""
        self.data_token = self._get_token(self.data_base_url, self.data_appkey, self.data_appsecret)

        if self.is_mock:
            # 모의투자 키가 실전과 동일한 경우 rate limit 방지를 위해 1초 대기
            if self.trade_appkey == self.data_appkey:
                time.sleep(1)
            self.trade_token = self._get_token(self.trade_base_url, self.trade_appkey, self.trade_appsecret)
        else:
            self.trade_token = self.data_token

        return self.data_token is not None and self.trade_token is not None

    def get_tr_id(self, base_tr_id):
        """실전/모의에 따른 TR-ID 변환"""
        if self.is_mock and base_tr_id.startswith('T'):
            return 'V' + base_tr_id[1:]
        return base_tr_id

    # ── 잔고 조회 (페이지네이션) ───────────────────────────────────────────

    def get_kr_balance(self):
        """국내주식 잔고 조회 (보유종목 + 예수금) — 다중 페이지 지원"""
        path  = "/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = self.get_tr_id("TTTC8434R")
        url   = f"{self.trade_base_url}{path}"

        account = self.trade_account or ""
        if "-" in account:
            parts        = account.split("-")
            cano         = parts[0]
            acnt_prdt_cd = parts[1] if len(parts) > 1 else "01"
        else:
            cano         = account[:8]
            acnt_prdt_cd = account[8:] if len(account) > 8 else "01"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.trade_token}",
            "appkey":        self.trade_appkey,
            "appsecret":     self.trade_appsecret,
            "tr_id":         tr_id,
            "custtype":      "P",
        }

        for auth_attempt in range(2):
            ctx_fk = ""
            ctx_nk = ""
            all_output1 = []
            output2     = {}

            expired_token = False

            for _ in range(10):  # 최대 10페이지
                params = {
                    "CANO":                cano,
                    "ACNT_PRDT_CD":        acnt_prdt_cd,
                    "AFHR_FLPR_YN":        "N",
                    "OFL_YN":              "",
                    "INQR_DVSN":           "02",
                    "UNPR_DVSN":           "01",
                    "FUND_STTL_ICLD_YN":   "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N",
                    "PRCS_DVSN":           "00",
                    "CTX_AREA_FK100":      ctx_fk,
                    "CTX_AREA_NK100":      ctx_nk,
                }
                try:
                    response = self.session.get(url, headers=headers, params=params)
                    if response.status_code != 200:
                        logger.error(f"❌ 국내 잔고 조회 실패 HTTP {response.status_code}: {response.text[:200]}")
                        return None
                    data = response.json()
                    if data.get('rt_cd') != '0':
                        if data.get('msg_cd') == 'EGW00123' and auth_attempt == 0:
                            logger.warning("⚠️ 토큰 만료 감지 — 주문 토큰 재발급 후 잔고 조회 재시도")
                            self._delete_cached_token(self.trade_appkey)
                            self.trade_token = self._get_token(
                                self.trade_base_url, self.trade_appkey, self.trade_appsecret
                            )
                            headers["authorization"] = f"Bearer {self.trade_token}"
                            expired_token = True
                            break
                        logger.error(f"❌ 국내 잔고 조회 실패 HTTP {response.status_code}: {response.text[:200]}")
                        return None

                    all_output1.extend(data.get('output1', []) or [])
                    if not output2:
                        output2 = data.get('output2', {})

                    # 다음 페이지 확인
                    tr_cont = (data.get('tr_cont') or response.headers.get('tr_cont', ''))
                    if tr_cont == 'M':
                        ctx_fk = data.get('ctx_area_fk100', '')
                        ctx_nk = data.get('ctx_area_nk100', '')
                        time.sleep(0.2)
                    else:
                        break
                except Exception as e:
                    logger.error(f"❌ 국내 잔고 조회 오류: {e}")
                    return None

            if expired_token:
                continue

            return {'rt_cd': '0', 'output1': all_output1, 'output2': output2}

        logger.error("❌ 국내 잔고 조회 실패: 토큰 재발급 후에도 만료 응답")
        return None

    # ── 기간별 시세 (FHKST03010100) ───────────────────────────────────────

    def get_kr_daily_ohlcv(self, symbol: str, lookback: int = 100) -> list:
        """
        국내주식 기간별시세 조회 (최대 100거래일).
        FHKST03010100 사용 — 날짜 범위 지정으로 SMA_60 등 장기 지표 계산 가능.
        반환: [{'date','open','high','low','close','volume'}, ...] 오래된 순
        """
        if not self.data_token:
            return []

        end_date   = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=int(lookback * 2))).strftime('%Y%m%d')

        url = f"{self.data_base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         symbol,
            "FID_INPUT_DATE_1":       start_date,
            "FID_INPUT_DATE_2":       end_date,
            "FID_PERIOD_DIV_CODE":    "D",
            "FID_ORG_ADJ_PRC":        "0",
        }

        for auth_attempt in range(2):
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.data_token}",
                "appkey":        self.data_appkey,
                "appsecret":     self.data_appsecret,
                "tr_id":         "FHKST03010100",
                "custtype":      "P",
            }
            try:
                res  = self.session.get(url, headers=headers, params=params, timeout=5)
                data = res.json()
                if data.get('rt_cd') != '0':
                    if data.get('msg_cd') == 'EGW00123' and auth_attempt == 0:
                        logger.warning("⚠️ 일봉 조회 data_token 만료 — 재발급 재시도")
                        self._delete_cached_token(self.data_appkey)
                        self.data_token = self._get_token(
                            self.data_base_url, self.data_appkey, self.data_appsecret
                        )
                        continue
                    return []

                rows = []
                for item in (data.get('output2') or []):
                    try:
                        rows.append({
                            'date':   item.get('stck_bsop_date', ''),
                            'open':   float(item.get('stck_oprc', 0) or 0),
                            'high':   float(item.get('stck_hgpr', 0) or 0),
                            'low':    float(item.get('stck_lwpr', 0) or 0),
                            'close':  float(item.get('stck_clpr', 0) or 0),
                            'volume': float(item.get('acml_vol', 0) or 0),
                        })
                    except Exception:
                        continue

                # API는 최신→과거 순 반환 — 오래된 순으로 뒤집기
                rows.reverse()
                return [r for r in rows if r['close'] > 0]
            except Exception as e:
                logger.debug(f"기간별 시세 조회 실패 ({symbol}): {e}")
                return []

        return []

    # ── 현재가 조회 (FHKST01010100) ───────────────────────────────────────

    def get_kr_current_price(self, symbol: str) -> float:
        """국내주식 현재가 단건 조회. 실패 시 0.0 반환."""
        if not self.data_token:
            return 0.0
        url = f"{self.data_base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         symbol,
        }
        for auth_attempt in range(2):
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.data_token}",
                "appkey":        self.data_appkey,
                "appsecret":     self.data_appsecret,
                "tr_id":         "FHKST01010100",
                "custtype":      "P",
            }
            try:
                res  = self.session.get(url, headers=headers, params=params, timeout=5)
                data = res.json()
                if data.get('rt_cd') == '0':
                    return float(data['output'].get('stck_prpr', 0) or 0)
                if data.get('msg_cd') == 'EGW00123' and auth_attempt == 0:
                    logger.warning("⚠️ 현재가 조회 data_token 만료 — 재발급 재시도")
                    self._delete_cached_token(self.data_appkey)
                    self.data_token = self._get_token(
                        self.data_base_url, self.data_appkey, self.data_appsecret
                    )
                    continue
                return 0.0
            except Exception as e:
                logger.debug(f"현재가 조회 실패 ({symbol}): {e}")
                return 0.0
        return 0.0

    # ── 주문가능금액 ──────────────────────────────────────────────────────

    def get_orderable_cash(self, symbol: str = "005930", price: int = 0) -> float:
        """KIS API에서 실제 주문가능금액 조회. 실패 시 -1 반환."""
        path  = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        tr_id = self.get_tr_id("TTTC8908R")
        url   = f"{self.trade_base_url}{path}"

        account = self.trade_account or ""
        if "-" in account:
            cano, acnt_prdt_cd = account.split("-")[0], account.split("-")[1]
        else:
            cano         = account[:8]
            acnt_prdt_cd = account[8:] if len(account) > 8 else "01"

        params = {
            "CANO":                  cano,
            "ACNT_PRDT_CD":          acnt_prdt_cd,
            "PDNO":                  symbol,
            "ORD_UNPR":              str(price),
            "ORD_DVSN":              "00",
            "CMA_EVLU_AMT_ICLD_YN":  "N",
            "OVRS_ICLD_YN":          "N",
        }

        for auth_attempt in range(2):
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.trade_token}",
                "appkey":        self.trade_appkey,
                "appsecret":     self.trade_appsecret,
                "tr_id":         tr_id,
                "custtype":      "P",
            }
            try:
                response = self.session.get(url, headers=headers, params=params)
                if response.status_code == 200:
                    result = response.json()
                    if result.get("rt_cd") == "0":
                        return float(result.get("output", {}).get("ord_psbl_cash", 0) or 0)
                    if result.get("msg_cd") == "EGW00123" and auth_attempt == 0:
                        logger.warning("⚠️ 주문가능금액 토큰 만료 — 재발급 재시도")
                        self._delete_cached_token(self.trade_appkey)
                        self.trade_token = self._get_token(
                            self.trade_base_url, self.trade_appkey, self.trade_appsecret
                        )
                        continue
                    logger.warning(f"⚠️ 주문가능금액 조회 실패: {result.get('msg1')}")
                    return -1.0
                logger.error(f"❌ 주문가능금액 조회 HTTP {response.status_code}: {response.text[:200]}")
                return -1.0
            except Exception as e:
                logger.error(f"❌ 주문가능금액 조회 오류: {e}")
                return -1.0

        return -1.0
