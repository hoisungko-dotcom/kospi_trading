from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests import Response

logger = logging.getLogger(__name__)

_TOKEN_TTL = 82_800
_REQUEST_RETRIES = 3
_TOKEN_MIN_INTERVAL_SEC = 65.0
_TOKEN_REFRESH_MARGIN_SEC = 600.0
_BALANCE_RETRIES = 4
_BALANCE_RETRY_SLEEP_SEC = 2.0
_ORDER_SETTLE_WAIT_SEC = 4.0
_ACCOUNT_CACHE_TTL_SEC = 2.5
_TOKEN_403_BACKOFFS = (5.0, 60.0, 300.0)


class KisMockDomesticBroker:
    def __init__(self) -> None:
        self.mode = (os.getenv("KIS_TRADING_MODE", "mock").strip().lower() or "mock")
        self.is_mock = self.mode != "live"
        if self.is_mock:
            self.base_url = os.getenv("KIS_MOCK_BASE_URL", "https://openapivts.koreainvestment.com:29443").rstrip("/")
            self.app_key = os.getenv("KIS_MOCK_APP_KEY") or os.getenv("KIS_MOCK_APPKEY") or ""
            self.app_secret = os.getenv("KIS_MOCK_APP_SECRET") or os.getenv("KIS_MOCK_APPSECRET") or ""
            account = (
                os.getenv("KIS_MOCK_CANO")
                or os.getenv("KIS_MOCK_ACCOUNT")
                or ""
            )
        else:
            self.base_url = os.getenv("KIS_REAL_BASE_URL", "https://openapi.koreainvestment.com:9443").rstrip("/")
            self.app_key = os.getenv("KIS_REAL_APP_KEY") or os.getenv("KIS_REAL_APPKEY") or ""
            self.app_secret = os.getenv("KIS_REAL_APP_SECRET") or os.getenv("KIS_REAL_APPSECRET") or ""
            account = (
                os.getenv("KIS_REAL_CANO")
                or os.getenv("KIS_REAL_ACCOUNT")
                or ""
            )
        self.cano = account
        self.acnt_prdt = os.getenv("KIS_MOCK_ACNT_PRDT_CD", "01")
        if "-" in self.cano:
            self.cano, self.acnt_prdt = self.cano.split("-", 1)
        if not self.app_key or not self.app_secret or not self.cano:
            raise RuntimeError(f"KIS {self.mode} credentials missing")
        cache_suffix = "mock" if self.is_mock else "live"
        self._token_cache_file = Path(__file__).parents[1] / "data" / f".kis_token_cache_{cache_suffix}_{self.cano}.json"
        self._token: str | None = None
        self._token_at = 0.0
        self._token_expires_at = 0.0
        self._last_token_attempt_at = 0.0
        self._account_snapshot_cache: tuple[float, dict, list[dict]] | None = None
        self.last_reject_message = ""
        self.last_error_message = ""
        self.order_path_enabled = True
        self.auth_fail_count = 0
        self._load_cached_token()
        logger.info("KIS 브로커 초기화 완료 (%s) 계좌=%s-%s", "모의" if self.is_mock else "실전", self.cano, self.acnt_prdt)

    def _disable_order_path(self, reason: str) -> None:
        self.order_path_enabled = False
        self.last_error_message = reason
        logger.error("🛑 KIS 주문 경로 비활성화: %s", reason)

    def _enable_order_path(self) -> None:
        if not self.order_path_enabled:
            logger.info("✅ KIS 주문 경로 재활성화")
        self.order_path_enabled = True

    def _cache_payload(self) -> dict:
        return {
            "token": self._token,
            "issued_at": datetime.fromtimestamp(self._token_at, tz=timezone.utc).isoformat() if self._token_at else "",
            "expires_at": datetime.fromtimestamp(self._token_expires_at, tz=timezone.utc).isoformat() if self._token_expires_at else "",
        }

    def _save_cached_token(self) -> None:
        if not self._token:
            return
        try:
            self._token_cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_cache_file.write_text(json.dumps(self._cache_payload()), encoding="utf-8")
        except Exception as exc:
            logger.warning("KIS 토큰 캐시 저장 실패: %s", exc)

    def _clear_cached_token(self) -> None:
        self._token = None
        self._token_at = 0.0
        self._token_expires_at = 0.0
        try:
            self._token_cache_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _load_cached_token(self) -> None:
        if not self._token_cache_file.exists():
            return
        try:
            data = json.loads(self._token_cache_file.read_text(encoding="utf-8"))
            token = str(data.get("token") or "").strip()
            issued_at = data.get("issued_at")
            expires_at = data.get("expires_at")
            if not token or not issued_at or not expires_at:
                return
            issued_ts = datetime.fromisoformat(issued_at).timestamp()
            expires_ts = datetime.fromisoformat(expires_at).timestamp()
            if time.time() >= (expires_ts - _TOKEN_REFRESH_MARGIN_SEC):
                logger.info("KIS 캐시 토큰 만료 임박 — 재발급 예정")
                return
            self._token = token
            self._token_at = issued_ts
            self._token_expires_at = expires_ts
            logger.info("✅ KIS 캐시 토큰 로드")
        except Exception as exc:
            logger.warning("KIS 토큰 캐시 로드 실패: %s", exc)

    def _issue_token(self, *, force: bool = False) -> bool:
        if not force and self._token and time.time() < (self._token_expires_at - _TOKEN_REFRESH_MARGIN_SEC):
            return True
        now = time.time()
        wait_sec = _TOKEN_MIN_INTERVAL_SEC - (now - self._last_token_attempt_at)
        if wait_sec > 0:
            logger.info("KIS %s토큰 재발급 대기 %.1fs", self.mode, wait_sec)
            time.sleep(wait_sec)
        self._last_token_attempt_at = time.time()
        last_error = ""
        for attempt, backoff in enumerate(_TOKEN_403_BACKOFFS, start=1):
            try:
                res = requests.post(
                    f"{self.base_url}/oauth2/tokenP",
                    json={
                        "grant_type": "client_credentials",
                        "appkey": self.app_key,
                        "appsecret": self.app_secret,
                    },
                    timeout=15,
                )
                if res.status_code == 403 and "EGW00133" in res.text:
                    last_error = f"403 EGW00133 token rate limit"
                    logger.warning("⚠️ KIS 403 (시도 %d/3) — %.0f초 대기", attempt, backoff)
                    if attempt < len(_TOKEN_403_BACKOFFS):
                        time.sleep(backoff)
                    continue
                data = self._json_or_raise(res)
                token = data.get("access_token")
                if not token:
                    last_error = f"KIS token missing: {data}"
                    break
                now_ts = time.time()
                self._token = token
                self._token_at = now_ts
                self._token_expires_at = now_ts + _TOKEN_TTL
                self.auth_fail_count = 0
                self._enable_order_path()
                self._save_cached_token()
                logger.info("KIS %s토큰 발급 완료", self.mode)
                return True
            except Exception as exc:
                last_error = str(exc)
                logger.error("KIS 토큰 발급 실패(attempt %d/3): %s", attempt, exc)
                if attempt < len(_TOKEN_403_BACKOFFS):
                    time.sleep(backoff)
        self.auth_fail_count += 1
        self._disable_order_path(f"KIS 인증 3회 실패: {last_error}")
        return False

    def _ensure_token(self) -> None:
        if not self._token:
            self._load_cached_token()
        if not self._token or time.time() >= (self._token_expires_at - _TOKEN_REFRESH_MARGIN_SEC):
            if not self._issue_token(force=True):
                raise RuntimeError(self.last_error_message or "KIS token unavailable")

    def _headers(self, tr_id: str) -> dict[str, str]:
        self._ensure_token()
        return {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self._token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _json_or_raise(self, response: Response) -> dict:
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def _request_with_token_retry(self, method: str, path: str, *, tr_id: str, json_body: dict | None = None, params: dict | None = None, timeout: int = 20) -> dict | None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                res = requests.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(tr_id),
                    json=json_body,
                    params=params,
                    timeout=timeout,
                )
                if res.status_code == 403 and "EGW00133" in res.text:
                    wait = _TOKEN_403_BACKOFFS[attempt - 1]
                    logger.warning("⚠️ KIS 403 (시도 %d/3) — %.0f초 대기", attempt, wait)
                    self._clear_cached_token()
                    if attempt < 3:
                        time.sleep(wait)
                        self._issue_token(force=True)
                        continue
                    self.auth_fail_count += 1
                    self._disable_order_path("KIS 인증 3회 실패 — 수동 확인 필요")
                    return None
                return self._json_or_raise(res)
            except Exception as exc:
                last_error = exc
                self.last_error_message = str(exc)
                if attempt < 3:
                    self._clear_cached_token()
                    time.sleep(_TOKEN_403_BACKOFFS[attempt - 1])
                    try:
                        self._issue_token(force=True)
                    except Exception:
                        pass
                    continue
        if last_error:
            logger.error("KIS token retry final failure: %s", last_error)
        return None

    def _request(self, method: str, path: str, *, tr_id: str, json_body: dict | None = None, params: dict | None = None, timeout: int = 20) -> dict | None:
        if not self.order_path_enabled and not self._token:
            logger.warning("KIS 주문 경로 비활성 상태 — 요청 생략: %s", path)
            return None
        last_error: Exception | None = None
        last_reject = ""
        self.last_reject_message = ""
        self.last_error_message = ""
        for attempt in range(1, _REQUEST_RETRIES + 1):
            try:
                time.sleep(0.7 if attempt == 1 else 1.0 * attempt)
                data = self._request_with_token_retry(
                    method,
                    path,
                    tr_id=tr_id,
                    json_body=json_body,
                    params=params,
                    timeout=timeout,
                )
                if not data:
                    return None
                if data.get("rt_cd") == "0":
                    return data
                last_reject = data.get("msg1", "") or "unknown reject"
                self.last_reject_message = last_reject
                logger.warning("KIS mock request rejected(attempt %d/%d): %s", attempt, _REQUEST_RETRIES, last_reject)
                if data.get("msg_cd") in {"EGW00121", "EGW00123", "EGW00133"}:
                    self._clear_cached_token()
                    if attempt < _REQUEST_RETRIES:
                        self._issue_token(force=True)
            except Exception as exc:
                last_error = exc
                self.last_error_message = str(exc)
                logger.error("KIS mock request failed(attempt %d/%d): %s", attempt, _REQUEST_RETRIES, exc)
                if attempt < _REQUEST_RETRIES:
                    self._clear_cached_token()
        if last_reject:
            logger.warning("KIS mock request final reject: %s", last_reject)
        elif last_error:
            logger.error("KIS mock request final failure: %s", last_error)
        return None

    def _tr_id(self, live_id: str) -> str:
        if self.is_mock and live_id.startswith("TT"):
            return "VT" + live_id[2:]
        return live_id

    def buy(self, symbol: str, qty: int) -> dict | None:
        return self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=self._tr_id("TTTC0802U"),
            json_body={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt,
                "PDNO": symbol,
                "ORD_DVSN": "01",
                "ORD_QTY": str(qty),
                "ORD_UNPR": "0",
            },
        )

    def buy_and_confirm(self, symbol: str, qty: int) -> tuple[dict | None, bool]:
        before_qty = 0
        for item in self.get_holdings():
            if item.get("code") == symbol:
                before_qty = int(item.get("qty", 0) or 0)
                break
        order = self.buy(symbol, qty)
        if not order:
            return None, False
        time.sleep(_ORDER_SETTLE_WAIT_SEC)
        holdings = self.get_holdings()
        current = next((item for item in holdings if item["code"] == symbol), None)
        if current is None:
            return order, False
        current_qty = int(current.get("qty", 0) or 0)
        return order, current_qty >= before_qty + qty

    def inquire_buyable(self, symbol: str, *, market_order: bool = True, price: float = 0.0) -> dict:
        ord_dvsn = "01" if market_order else "00"
        ord_unpr = "0" if market_order else str(int(price))
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=self._tr_id("TTTC8908R"),
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt,
                "PDNO": symbol,
                "ORD_UNPR": ord_unpr,
                "ORD_DVSN": ord_dvsn,
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
            timeout=30,
        )
        output = data.get("output") if data else {}
        return output if isinstance(output, dict) else {}

    def inquire_buyable_qty(self, symbol: str, *, market_order: bool = True, price: float = 0.0) -> int:
        output = self.inquire_buyable(symbol, market_order=market_order, price=price)
        qty_keys = (
            "nrcvb_buy_qty",
            "max_buy_qty",
            "ord_psbl_qty",
            "max_ord_psbl_qty",
        )
        for key in qty_keys:
            value = output.get(key)
            if value not in (None, ""):
                try:
                    return int(float(str(value).replace(",", "").strip()))
                except Exception:
                    continue
        return 0

    def sell(self, symbol: str, qty: int) -> dict | None:
        return self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=self._tr_id("TTTC0801U"),
            json_body={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt,
                "PDNO": symbol,
                "ORD_DVSN": "01",
                "ORD_QTY": str(qty),
                "ORD_UNPR": "0",
            },
        )

    def sell_and_confirm(self, symbol: str, qty: int) -> tuple[dict | None, bool]:
        order = self.sell(symbol, qty)
        if not order:
            return None, False
        time.sleep(_ORDER_SETTLE_WAIT_SEC)
        holdings = self.get_holdings()
        remaining = next((item for item in holdings if item["code"] == symbol), None)
        if remaining is None:
            return order, True
        if int(remaining.get("qty", 0) or 0) < qty:
            logger.info("KIS 모의매도 부분반영 확인: %s qty=%s -> remaining=%s", symbol, qty, remaining.get("qty"))
        return order, False

    def _fetch_account_snapshot(self) -> tuple[dict, list[dict]]:
        params = {
            "CANO": self.cano,
            "ACNT_PRDT_CD": self.acnt_prdt,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        data = None
        for attempt in range(1, _BALANCE_RETRIES + 1):
            data = self._request(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id=self._tr_id("TTTC8434R"),
                params=params,
                timeout=60,
            )
            if data:
                break
            if attempt < _BALANCE_RETRIES:
                logger.warning("KIS 계좌조회 재시도 %d/%d", attempt + 1, _BALANCE_RETRIES)
                time.sleep(_BALANCE_RETRY_SLEEP_SEC * attempt)
        if not data:
            return {}, []

        output2 = data.get("output2") or [{}]
        output2 = output2[0] if isinstance(output2, list) and output2 else output2
        balance = {
            "cash": float(output2.get("prvs_rcdl_excc_amt", 0) or 0),
            "deposit_total": float(output2.get("dnca_tot_amt", 0) or 0),
            "stock_value": float(output2.get("scts_evlu_amt", 0) or 0),
            "total_assets": float(output2.get("nass_amt", 0) or 0),
        }
        holdings = []
        for row in data.get("output1") or []:
            qty = int(row.get("hldg_qty", 0) or 0)
            if qty <= 0:
                continue
            holdings.append({
                "code": row.get("pdno", ""),
                "name": row.get("prdt_name", row.get("pdno", "")),
                "qty": qty,
                "entry_price": float(row.get("pchs_avg_pric", 0) or 0),
                "current_price": float(row.get("prpr", 0) or 0),
            })
        return balance, holdings

    def get_account_snapshot(self, *, force: bool = False) -> tuple[dict, list[dict]]:
        now = time.time()
        if not force and self._account_snapshot_cache:
            cached_at, balance, holdings = self._account_snapshot_cache
            if now - cached_at <= _ACCOUNT_CACHE_TTL_SEC:
                return balance, holdings
        balance, holdings = self._fetch_account_snapshot()
        self._account_snapshot_cache = (now, balance, holdings)
        return balance, holdings

    def get_balance(self) -> dict:
        balance, _ = self.get_account_snapshot()
        return balance

    def get_holdings(self) -> list[dict]:
        _, holdings = self.get_account_snapshot()
        return holdings

    def get_volume_rank(self, *, market: str = "0001") -> list[dict]:
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            tr_id="FHPST01710000",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": market,
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "0",
                "FID_VOL_CNT": "0",
                "FID_INPUT_DATE_1": "0",
            },
            timeout=30,
        )
        return list(data.get("output") or []) if data else []

    def get_volume_power(self, *, market: str = "0001") -> list[dict]:
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/ranking/volume-power",
            tr_id="FHPST01680000",
            params={
                "fid_trgt_exls_cls_code": "0",
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20168",
                "fid_input_iscd": market,
                "fid_div_cls_code": "0",
                "fid_input_price_1": "0",
                "fid_input_price_2": "0",
                "fid_vol_cnt": "0",
                "fid_trgt_cls_code": "0",
            },
            timeout=30,
        )
        return list(data.get("output") or []) if data else []

    def get_foreign_institution_total(self, *, market: str = "0001") -> list[dict]:
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            tr_id="FHPTJ04400000",
            params={
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": market,
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_ETC_CLS_CODE": "0",
            },
            timeout=30,
        )
        return list(data.get("output") or []) if data else []
