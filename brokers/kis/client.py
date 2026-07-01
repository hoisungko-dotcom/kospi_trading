"""
KIS API 클라이언트 (국내 주식 전용)
기존 KISClient + MarketDataKOSPI + OrderExecution을 통합한 단일 인터페이스.
"""
import os
import logging
import time
import pandas as pd
from dotenv import load_dotenv

from brokers.kis.api_client import KISClient
from services.market_data import MarketDataKOSPI
from services.order_execution import OrderExecution
from strategy.investor_flow import InvestorFlow

load_dotenv(override=True)
logger = logging.getLogger(__name__)


class KISClientKospi:
    """국내 주식 KIS API 통합 클라이언트"""

    def __init__(self):
        self.broker_name = "kis"
        self._client = KISClient()
        self._client.authenticate()
        self._market_data = MarketDataKOSPI(self._client)
        self._executor = OrderExecution(self._client)
        self.is_mock = os.getenv("MOCK_TRADING", "true").lower() == "true"
        self._last_api_call_t = 0.0
        self._api_min_interval = float(os.getenv("KIS_SERIAL_API_MIN_INTERVAL", "0.35") or 0.35)
        logger.info(f"✅ KISClientKospi 초기화 완료 ({'모의' if self.is_mock else '실전'}투자)")

    def _api_wait(self, label: str = "") -> None:
        elapsed = time.time() - self._last_api_call_t
        if elapsed < self._api_min_interval:
            time.sleep(self._api_min_interval - elapsed)
        self._last_api_call_t = time.time()

    # ── OHLCV + 지표 ─────────────────────────────────────────────────────

    def _compute_indicators(self, df: pd.DataFrame, symbol: str = '') -> dict | None:
        """DataFrame(date,open,high,low,close,volume) → 지표 dict"""
        try:
            if df is None or df.empty or len(df) < 20:
                return None

            close  = df['close'].astype(float)
            high   = df['high'].astype(float)
            low    = df['low'].astype(float)
            volume = df['volume'].astype(float)

            sma_5   = close.rolling(5).mean()
            sma_20  = close.rolling(20).mean()
            sma_52  = close.rolling(52).mean()
            sma_60  = close.rolling(60).mean()
            sma_224 = close.rolling(224).mean()
            sma_448 = close.rolling(448).mean()

            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = -delta.clip(upper=0).rolling(14).mean()
            rsi   = 100 - (100 / (1 + gain / loss.replace(0, float('nan'))))

            ema12       = close.ewm(span=12, adjust=False).mean()
            ema26       = close.ewm(span=26, adjust=False).mean()
            macd_line   = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()

            bb_mid   = close.rolling(20).mean()
            bb_upper = bb_mid + 2 * close.rolling(20).std()
            avg_vol  = volume.rolling(20).mean()

            tr  = pd.concat([high - low,
                              abs(high - close.shift()),
                              abs(low  - close.shift())], axis=1).max(axis=1)
            atr = tr.rolling(14).mean()

            def s(series, idx=-1, default=0.0):
                try:
                    v = series.iloc[idx]
                    return float(v) if pd.notna(v) else default
                except Exception:
                    return default

            # 연속 매집 일수 (기관/외인 수급 프록시 — price/volume 기반)
            ph = df[['open', 'high', 'low', 'close', 'volume']].tail(30).to_dict('records')
            consecutive_buy_days = InvestorFlow.buy_pressure_days(ph)

            return {
                'timestamp':           df['date'].iloc[-1] if 'date' in df.columns else '',
                'name':                symbol,
                'close':               s(close),
                'open':                s(df['open'].astype(float)),
                'high':                s(high),
                'low':                 s(low),
                'volume':              s(volume, -2, default=s(volume)),  # 전일 완성봉 기준 (장중 미완성봉 제외)
                'consecutive_buy_days': consecutive_buy_days,
                'sma_5':            s(sma_5,        default=s(close)),
                'sma_20':           s(sma_20,       default=s(close)),
                'sma_52':           s(sma_52,       default=0.0),
                'sma_60':           s(sma_60,       default=s(close)),
                'sma_224':          s(sma_224,      default=0.0),
                'sma_224_prev':     s(sma_224, -2,  default=0.0),
                'sma_448':          s(sma_448,      default=0.0),
                'close_prev':       s(close,   -2,  default=s(close)),
                'rsi':              s(rsi,          default=50.0),
                'rsi_prev':         s(rsi, -2,      default=50.0),
                'macd':             s(macd_line),
                'macd_signal':      s(signal_line),
                'macd_prev':        s(macd_line,   -2),
                'macd_signal_prev': s(signal_line, -2),
                'avg_volume_20':    s(avg_vol,      default=s(volume)),
                'bb_upper':         s(bb_upper,     default=s(close)),
                'bb_middle':        s(bb_mid,       default=s(close)),
                'close_5d_ago':     s(close, -5,    default=s(close)),
                'close_20d_ago':    s(close, -20,   default=s(close)),
                'high_20d':         float(high.tail(20).max()),
                'low_52w':          float(low.tail(252).min()),
                'atr':              s(atr,          default=s(close) * 0.02),
                'stop_loss':        0.0,
            }
        except Exception as e:
            logger.debug(f"지표 계산 실패 ({symbol}): {e}")
            return None

    def get_bulk_daily_ohlcv(self, symbols: list[str], kospi_set: set = None) -> list[dict]:
        """
        국내주식 일봉 대량 수집.

        Yahoo Finance의 .KS/.KQ 데이터 누락이 잦아 스크리닝 데이터도 KIS 실전
        기간별시세를 1순위로 사용한다. KIS 실패 시 MarketDataKOSPI 내부의 pykrx
        보조 경로가 한 번 더 시도된다.
        반환: [{'symbol','name','data','error'}, ...]
        """
        import time as _time

        results: list[dict] = []
        success = 0
        total = len(symbols)
        logger.info(f"📡 KIS 국내 기간별시세로 {total}개 종목 수집 중...")

        for idx, sym in enumerate(symbols, 1):
            df = self._market_data.get_kospi_ohlcv(sym, interval='1d', lookback=500)
            data = self._compute_indicators(df, sym)
            if data is not None:
                success += 1
            results.append({'symbol': sym, 'name': sym, 'data': data, 'error': None})

            if idx % 100 == 0 or idx == total:
                logger.info(f"  진행: {idx}/{total} | 성공 {success}개")

            # 실전 시세 API는 20건/초 제한. 한국/미국 봇 동시 실행 여지를 두고 약 10건/초로 운용.
            _time.sleep(float(os.getenv('KIS_DATA_REQUEST_INTERVAL', '0.10')))

        logger.info(f"✅ KIS 일봉 수집 완료: {success}/{total}개 성공")
        return results

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        """
        장중 실시간 현재가 일괄 조회 (KIS API).
        심볼당 1회 호출, rate limit 방지 50ms 간격.
        """
        import time as _time
        prices: dict[str, float] = {}
        for sym in symbols:
            self._api_wait("current_price")
            price = self._market_data.get_current_price(sym)
            if price:
                prices[sym] = price
            _time.sleep(0.05)
        return prices

    def get_daily_ohlcv(self, symbol: str) -> dict | None:
        """단일 종목 일봉 + 지표 (realtime_monitoring 용)"""
        df = self._market_data.get_kospi_ohlcv(symbol, interval='1d', lookback=500)
        return self._compute_indicators(df, symbol)

    def get_intraday_ohlcv(self, symbol: str, interval: str = '1m', lookback: int = 30):
        """단일 종목 분봉 데이터. 실패 시 None."""
        self._api_wait("intraday")
        return self._market_data.get_kospi_ohlcv(symbol, interval=interval, lookback=lookback)

    def get_foreign_net_buying(self, symbol: str, lookback: int = 5) -> list[dict]:
        """종목별 외국계 순매수추이 (FHKST644400C0).
        반환: [{'date': 'YYYYMMDD', 'foreigner_net': float, 'net_flow': float}, ...]
              최신 데이터가 마지막 인덱스. 실패 시 빈 리스트.
        """
        import requests
        try:
            client = self._client
            url = f"{client.data_base_url}/uapi/domestic-stock/v1/quotations/frgnmem-pchs-trend"
            params = {
                "FID_INPUT_ISCD":        symbol,
                "FID_INPUT_ISCD_2":      "99999",  # 외국계 전체
                "FID_COND_MRKT_DIV_CODE": "J",
            }
            for attempt in range(2):
                self._api_wait("foreign_flow")
                headers = {
                    "content-type":  "application/json; charset=utf-8",
                    "authorization": f"Bearer {client.data_token}",
                    "appkey":        client.data_appkey,
                    "appsecret":     client.data_appsecret,
                    "tr_id":         "FHKST644400C0",
                    "custtype":      "P",
                }
                resp   = requests.get(url, headers=headers, params=params, timeout=8)
                result = resp.json()
                if result.get('rt_cd') == '0':
                    break
                if result.get('msg_cd') == 'EGW00123' and attempt == 0:
                    client._delete_cached_token(client.data_appkey)
                    client.data_token = client._get_token(
                        client.data_base_url, client.data_appkey, client.data_appsecret
                    )
                    continue
                logger.debug(f"외국계 순매수 조회 실패 ({symbol}): {result.get('msg1', '')}")
                return []

            rows = result.get('output') or result.get('output1') or []
            flow = []
            for row in rows[:lookback]:
                # KIS 응답 필드명 다중 시도
                net = (
                    row.get('frgn_ntby_qty')
                    or row.get('frgn_net_buy')
                    or row.get('ntby_qty')
                    or row.get('frgn_shnu_qty', 0)
                )
                try:
                    net = float(str(net).replace(',', ''))
                except Exception:
                    net = 0.0
                flow.append({
                    'date':          row.get('stck_bsop_date', ''),
                    'foreigner_net': net,
                    'institution_net': 0,
                    'net_flow':      net,
                })
            # 오래된 날짜 → 최신 순으로 정렬 ([-1]이 최신)
            flow.sort(key=lambda x: x.get('date', ''))
            return flow
        except Exception as e:
            logger.debug(f"외국계 순매수 조회 예외 ({symbol}): {e}")
            return []

    def get_orderable_cash(self, symbol: str, price: float, use_max: bool = False) -> float:
        """KIS API에서 실제 주문가능금액 조회 (당일 매도 재사용 포함 최대 매수가능금액)"""
        self._api_wait("orderable_cash")
        return self._client.get_orderable_cash(symbol=symbol, price=int(price), use_max=use_max)

    def verify_domestic_fill(
        self,
        symbol: str,
        side: str,
        previous_qty: int,
        order_qty: int,
        retries: int | None = None,
        delay_sec: float | None = None,
    ) -> bool | str:
        """주문 전송 후 실제 잔고 수량 변화로 체결 여부를 확인한다.

        KIS 주문 API의 성공 응답은 주문 접수 성공이지 체결 확정이 아닐 수 있다.
        그래서 로컬 포트폴리오는 잔고 변화가 확인된 뒤에만 갱신한다.
        """
        retries = retries if retries is not None else int(os.getenv("ORDER_FILL_VERIFY_RETRIES", "6") or 6)
        delay_sec = delay_sec if delay_sec is not None else float(os.getenv("ORDER_FILL_VERIFY_DELAY_SEC", "3.0") or 3.0)
        side = side.upper()
        expected_buy_qty = previous_qty + order_qty
        expected_sell_qty = max(0, previous_qty - order_qty)
        balance_failures = 0

        for attempt in range(1, retries + 1):
            time.sleep(delay_sec)
            balance = self.get_balance()
            if not balance:
                balance_failures += 1
                logger.warning(
                    f"⏳ [{symbol}] {side} 체결 확인 보류: 잔고 조회 실패 "
                    f"(시도 {attempt}/{retries})"
                )
                continue
            holdings = balance.get('holdings', {}) if balance else {}
            current_qty = int(holdings.get(symbol, {}).get('quantity', 0) or 0)

            if side == "BUY" and current_qty >= expected_buy_qty:
                logger.info(
                    f"✅ [{symbol}] 매수 체결 확인: {previous_qty}주 → {current_qty}주 "
                    f"(시도 {attempt}/{retries})"
                )
                return True
            if side == "SELL" and current_qty <= expected_sell_qty:
                logger.info(
                    f"✅ [{symbol}] 매도 체결 확인: {previous_qty}주 → {current_qty}주 "
                    f"(시도 {attempt}/{retries})"
                )
                return True

            logger.warning(
                f"⏳ [{symbol}] {side} 체결 대기: 현재 {current_qty}주, "
                f"기대 {'>=' + str(expected_buy_qty) if side == 'BUY' else '<=' + str(expected_sell_qty)}주 "
                f"(시도 {attempt}/{retries})"
            )

        if balance_failures >= retries:
            logger.error(
                f"⚠️ [{symbol}] {side} 체결 확인 불가 — 주문은 접수됐을 수 있으나 "
                "잔고 조회가 모두 실패했습니다. 재주문하지 않고 보류합니다."
            )
            return "PENDING"

        logger.error(
            f"❌ [{symbol}] {side} 체결 미확인 — 주문은 접수됐을 수 있으나 "
            "잔고 변화가 확인되지 않아 로컬 포트폴리오는 갱신하지 않습니다."
        )
        return False


    # ── 잔고 동기화 ───────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """
        KIS API에서 실제 계좌 잔고 조회.
        반환: {'cash': float, 'holdings': {symbol: {'quantity','price','amount','highest_price'}}}
        실패 시 빈 dict 반환.
        """
        try:
            self._api_wait("balance")
            res = self._client.get_kr_balance()
            if not res or res.get('rt_cd') != '0':
                logger.warning(f"⚠️ 잔고 조회 응답 이상: {res}")
                return {}

            # 예수금(dnca_tot_amt) = KIS 앱 실제 잔고와 동일한 값
            # 주문가능금액(max_buy_amt)은 주문 직전에 get_orderable_cash()로 별도 조회
            output2 = res.get('output2', {})
            if isinstance(output2, list):
                output2 = output2[0] if output2 else {}
            cash = float(output2.get('dnca_tot_amt', 0) or 0)

            # 보유종목
            holdings = {}
            for h in res.get('output1', []):
                symbol = h.get('pdno', '').strip()
                qty    = int(float(h.get('hldg_qty', 0) or 0))
                if not symbol or qty <= 0:
                    continue
                avg_price = float(h.get('pchs_avg_pric', 0) or 0)
                cur_price = float(h.get('prpr', 0) or avg_price)
                holdings[symbol] = {
                    'quantity':      qty,
                    'price':         avg_price,
                    'amount':        avg_price * qty,
                    'highest_price': cur_price,
                }

            logger.info(
                f"✅ KIS 잔고 조회 완료: 예수금 ₩{cash:,.0f}, "
                f"보유종목 {len(holdings)}개"
            )
            return {'cash': cash, 'holdings': holdings}

        except Exception as e:
            logger.error(f"❌ KIS 잔고 조회 실패: {e}")
            return {}

    # ── 주문 ─────────────────────────────────────────────────────────────

    def place_buy_order(
        self,
        symbol: str,
        quantity: int,
        price: float,
        allow_price_chase: bool = False,
        market_order: bool = False,
    ) -> bool:
        """매수 주문 (market_order=True 시 시장가)"""
        return self._executor.execute_order(
            symbol,
            quantity,
            price,
            side='BUY',
            allow_price_chase=allow_price_chase,
            market_order=market_order,
        )

    def place_sell_order(self, symbol: str, quantity: int, price: float, market_order: bool = False) -> bool:
        """매도 주문 (market_order=True 시 시장가)"""
        return self._executor.execute_order(symbol, quantity, price, side='SELL', market_order=market_order)
