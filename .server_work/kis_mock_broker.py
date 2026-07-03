from __future__ import annotations

import logging
import os
import time

import requests
from requests import Response

logger = logging.getLogger(__name__)

_BASE_URL = "https://openapivts.koreainvestment.com:29443"
_TOKEN_TTL = 82_800
_REQUEST_RETRIES = 3
_TOKEN_MIN_INTERVAL_SEC = 65.0
_BALANCE_RETRIES = 4
_BALANCE_RETRY_SLEEP_SEC = 2.0
_ORDER_SETTLE_WAIT_SEC = 4.0
_ACCOUNT_CACHE_TTL_SEC = 2.5


class KisMockDomesticBroker:
    def __init__(self) -> None:
        self.app_key = os.getenv("KIS_MOCK_APP_KEY") or os.getenv("KIS_MOCK_APPKEY") or ""
        self.app_secret = os.getenv("KIS_MOCK_APP_SECRET") or os.getenv("KIS_MOCK_APPSECRET") or ""
        account = (
            os.getenv("KIS_MOCK_CANO")
            or os.getenv("KIS_MOCK_ACCOUNT")
            or ""
        )
        self.cano = account
        self.acnt_prdt = os.getenv("KIS_MOCK_ACNT_PRDT_CD", "01")
        if "-" in self.cano:
            self.cano, self.acnt_prdt = self.cano.split("-", 1)
        if not self.app_key or not self.app_secret or not self.cano:
            raise RuntimeError("KIS mock credentials missing")
        self._token: str | None = None
        self._token_at = 0.0
        self._last_token_attempt_at = 0.0
        self._account_snapshot_cache: tuple[float, dict, list[dict]] | None = None
        self.last_reject_message = ""
        self.last_error_message = ""
        self._issue_token()

    def _issue_token(self) -> None:
        now = time.time()
        wait_sec = _TOKEN_MIN_INTERVAL_SEC - (now - self._last_token_attempt_at)
        if wait_sec > 0:
            logger.info("KIS 모의토큰 재발급 대기 %.1fs", wait_sec)
            time.sleep(wait_sec)
        self._last_token_attempt_at = time.time()
        res = requests.post(
            f"{_BASE_URL}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
            timeout=15,
        )
        data = self._json_or_raise(res)
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"KIS token missing: {data}")
        self._token = token
        self._token_at = time.time()
        logger.info("KIS 모의토큰 발급 완료")

    def _ensure_token(self) -> None:
        if not self._token or (time.time() - self._token_at) > _TOKEN_TTL:
            self._issue_token()

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

    def _request(self, method: str, path: str, *, tr_id: str, json_body: dict | None = None, params: dict | None = None, timeout: int = 20) -> dict | None:
        last_error: Exception | None = None
        last_reject = ""
        self.last_reject_message = ""
        self.last_error_message = ""
        for attempt in range(1, _REQUEST_RETRIES + 1):
            try:
                time.sleep(0.7 if attempt == 1 else 1.0 * attempt)
                res = requests.request(
                    method,
                    f"{_BASE_URL}{path}",
                    headers=self._headers(tr_id),
                    json=json_body,
                    params=params,
                    timeout=timeout,
                )
                data = self._json_or_raise(res)
                if data.get("rt_cd") == "0":
                    return data
                last_reject = data.get("msg1", "") or "unknown reject"
                self.last_reject_message = last_reject
                logger.warning("KIS mock request rejected(attempt %d/%d): %s", attempt, _REQUEST_RETRIES, last_reject)
                if data.get("msg_cd") in {"EGW00121", "EGW00123", "EGW00133"}:
                    self._token = None
                    if attempt < _REQUEST_RETRIES:
                        self._issue_token()
            except Exception as exc:
                last_error = exc
                self.last_error_message = str(exc)
                logger.error("KIS mock request failed(attempt %d/%d): %s", attempt, _REQUEST_RETRIES, exc)
                if attempt < _REQUEST_RETRIES:
                    self._token = None
        if last_reject:
            logger.warning("KIS mock request final reject: %s", last_reject)
        elif last_error:
            logger.error("KIS mock request final failure: %s", last_error)
        return None

    def buy(self, symbol: str, qty: int) -> dict | None:
        return self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id="VTTC0802U",
            json_body={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt,
                "PDNO": symbol,
                "ORD_DVSN": "01",
                "ORD_QTY": str(qty),
                "ORD_UNPR": "0",
            },
        )

    def sell(self, symbol: str, qty: int) -> dict | None:
        return self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id="VTTC0801U",
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
                tr_id="VTTC8434R",
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
