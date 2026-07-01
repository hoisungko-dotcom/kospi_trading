import json
import logging
import os
import time
import traceback

import requests

logger = logging.getLogger(__name__)

RATE_LIMIT_CODE = "EGW00201"
TOKEN_EXPIRED_CODE = "EGW00123"
PRICE_LIMIT_CODE = "40270000"
MAX_RETRIES = 5
RETRY_DELAY_SEC = 1.0
TOKEN_RETRY_DELAY = 3.0


class OrderExecution:
    """주문 실행 모듈 (활성 브로커의 trade 세션 사용)."""

    def __init__(self, broker_client):
        self.client = broker_client
        self._last_token_refresh: float = time.time()

    def _refresh_order_token(self) -> str | None:
        if hasattr(self.client, "refresh_trade_token"):
            token = self.client.refresh_trade_token(force=True)
            if token:
                self._last_token_refresh = time.time()
            return token
        if hasattr(self.client, "_delete_cached_token"):
            self.client._delete_cached_token(self.client.trade_appkey)
        if hasattr(self.client, "_get_token"):
            token = self.client._get_token(
                self.client.trade_base_url,
                self.client.trade_appkey,
                self.client.trade_appsecret,
                force=True,
            )
            if token:
                self.client.trade_token = token
                self._last_token_refresh = time.time()
                return token
        return None

    def _ensure_fresh_token(self) -> bool:
        stale_threshold = 6 * 3600
        if time.time() - self._last_token_refresh > stale_threshold:
            logger.info("🔄 주문 토큰 선제 갱신 (6시간 경과)")
            return bool(self._refresh_order_token())
        return True

    def execute_order(self, symbol, qty, price, side='BUY', allow_price_chase: bool | None = None, market_order: bool = False):
        sep = '=' * 80
        logger.critical(f"\n{sep}")
        logger.critical(f"🚀 브로커 API {side} 주문 시작")
        logger.critical(f"{sep}")
        logger.critical(f"  Symbol       : {symbol}")
        logger.critical(f"  Qty          : {qty}주")
        logger.critical(f"  Price        : ₩{price:,.0f}")
        logger.critical(f"  Account      : {getattr(self.client, 'trade_account', 'N/A')}")
        logger.critical(f"  Token Valid  : {bool(getattr(self.client, 'trade_token', None))}")

        self._ensure_fresh_token()
        base_tr_id = "TTTC0012U" if side == 'BUY' else "TTTC0011U"
        tr_id = self.client.get_tr_id(base_tr_id)
        url = f"{self.client.trade_base_url}/uapi/domestic-stock/v1/trading/order-cash"
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.client.trade_token}",
            "appkey": self.client.trade_appkey,
            "appsecret": self.client.trade_appsecret,
            "tr_id": tr_id,
            "custtype": "P",
        }

        def krx_tick(p: float) -> int:
            if p < 2_000:
                return 1
            if p < 5_000:
                return 5
            if p < 20_000:
                return 10
            if p < 50_000:
                return 50
            if p < 200_000:
                return 100
            if p < 500_000:
                return 500
            return 1_000

        buy_slippage = float(os.getenv("BUY_PRICE_SLIPPAGE_PCT", "0.0") or 0.0)
        sell_slippage = float(os.getenv("SELL_PRICE_SLIPPAGE_PCT", "0.005") or 0.005)
        allow_buy_chase = (
            os.getenv("ALLOW_BUY_PRICE_CHASE", "false").lower() == "true"
            if allow_price_chase is None else bool(allow_price_chase)
        )

        def order_price_from(base_price: float, chase: bool) -> tuple[int, int]:
            if side == 'BUY':
                slipped_price = base_price * (1 + max(0.0, buy_slippage)) if chase else base_price * (1 + min(0.0, buy_slippage))
            else:
                slipped_price = base_price * (1 - max(0.0, sell_slippage))
            tick = krx_tick(slipped_price)
            return max(int(slipped_price // tick * tick), tick), tick

        ord_price, unit = order_price_from(price, allow_buy_chase)
        no_chase_price, no_chase_unit = order_price_from(price, False)
        account = self.client.trade_account or ""
        if "-" in account:
            parts = account.split("-")
            cano, acnt = parts[0], (parts[1] if len(parts) > 1 else "01")
        else:
            cano, acnt = account[:8], (account[8:] if len(account) > 8 else "01")
        exchange_id = os.getenv("KIS_EXCHANGE_ID", "KRX").strip() or "KRX"

        if market_order:
            ord_dvsn = "01"
            ord_unpr = "0"
            order_label = "시장가"
        else:
            ord_dvsn = "00"
            ord_unpr = str(ord_price)
            order_label = '강한신호 소폭 추격 지정가' if side == 'BUY' and allow_buy_chase else ('현재가 이하 지정가' if side == 'BUY' else '체결 우선 지정가')

        data = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": ord_unpr,
            "CNDT_PRIC": "",
            "SLL_TYPE": "",
            "EXCG_ID_DVSN_CD": exchange_id,
        }

        logger.critical(f"  URL          : {url}")
        logger.critical(f"  TR_ID        : {tr_id}")
        logger.critical(f"  Order Price  : ₩{ord_price:,.0f} ({order_label})")
        logger.critical(f"  Data         : {json.dumps(data, ensure_ascii=False)}")

        for attempt in range(1, MAX_RETRIES + 1):
            logger.critical(f"  ⏳ API 호출 중... (시도 {attempt}/{MAX_RETRIES})")
            try:
                time.sleep(0.5)
                response = requests.post(url, headers=headers, data=json.dumps(data), timeout=20)
                result = response.json()
                logger.critical(f"\n📨 브로커 API 응답 (시도 {attempt}):")
                logger.critical(f"  HTTP Status  : {response.status_code}")
                logger.critical(f"  rt_cd        : {result.get('rt_cd')}")
                logger.critical(f"  msg1         : {result.get('msg1')}")
                logger.critical(f"  msg2         : {result.get('msg2')}")
                logger.critical(f"  Full Response:\n{json.dumps(result, indent=2, ensure_ascii=False)}")

                if result.get('rt_cd') == '0':
                    logger.critical(f"\n✅ {side} 주문 성공!  {symbol}  {qty}주")
                    logger.critical(f"{sep}\n")
                    return True

                upper_limit_rejected = side == 'BUY' and attempt < MAX_RETRIES and "상한가" in str(result.get('msg1', '')) and ord_price > no_chase_price
                if upper_limit_rejected:
                    old_price = ord_price
                    ord_price = no_chase_price
                    unit = no_chase_unit
                    data["ORD_UNPR"] = str(ord_price)
                    logger.critical(f"  ⚠️ 상한가 초과 — 추격가 해제 후 현재가 이하 지정가 재시도 (₩{old_price:,.0f} → ₩{ord_price:,.0f})")
                    time.sleep(RETRY_DELAY_SEC)
                    continue

                if side == 'BUY' and result.get('msg_cd') == PRICE_LIMIT_CODE and attempt < MAX_RETRIES and ord_price > unit:
                    old_price = ord_price
                    ord_price = max(unit, ord_price - unit)
                    data["ORD_UNPR"] = str(ord_price)
                    logger.critical(f"  ⚠️ 모의투자 가격 오류 — 매수 주문가 한 호가 낮춰 재시도 (₩{old_price:,.0f} → ₩{ord_price:,.0f})")
                    time.sleep(RETRY_DELAY_SEC)
                    continue

                token_expired = result.get('msg_cd') == TOKEN_EXPIRED_CODE or '만료된 token' in str(result.get('msg1', '')) or 'expired' in str(result.get('msg1', '')).lower()
                if token_expired:
                    if attempt < MAX_RETRIES:
                        logger.critical(f"  ⚠️ 토큰 만료 감지 (시도 {attempt}) — 재발급 중...")
                        try:
                            new_token = self._refresh_order_token()
                            if new_token:
                                headers["authorization"] = f"Bearer {new_token}"
                                logger.critical("  ✅ 토큰 재발급 성공 — 재시도")
                            else:
                                logger.critical(f"  ⚠️ 토큰 재발급 실패 — {TOKEN_RETRY_DELAY}초 후 재시도")
                        except Exception as auth_e:
                            logger.critical(f"  ❌ 토큰 재발급 예외: {auth_e}")
                        time.sleep(TOKEN_RETRY_DELAY)
                        continue
                    logger.critical("  ❌ 토큰 만료 — 재시도 횟수 소진")
                    break

                if result.get('msg_cd') == RATE_LIMIT_CODE and attempt < MAX_RETRIES:
                    logger.critical(f"  ⚠️ 초당 거래건수 초과 — {RETRY_DELAY_SEC}초 후 재시도...")
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                break
            except requests.exceptions.Timeout:
                logger.critical(f"\n❌ 타임아웃: 브로커 API 응답 없음 (시도 {attempt})")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                break
            except (requests.exceptions.RequestException, Exception) as e:
                logger.critical(f"\n❌ 예외 발생: {type(e).__name__}: {e}")
                if attempt < MAX_RETRIES:
                    logger.critical(f"  ⚠️ {RETRY_DELAY_SEC}초 후 재시도...")
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                logger.critical(f"\nTraceback:\n{traceback.format_exc()}")
        return False
