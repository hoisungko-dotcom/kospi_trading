import re
import pandas as pd
import numpy as np
import logging
import yfinance as yf
from datetime import datetime, timedelta
import requests
# pykrx 제거 — KIS 실패 시 yfinance fallback 사용 (인증 불필요)

logger = logging.getLogger(__name__)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# 6자리 숫자로만 이루어진 정상 종목코드 패턴 (우선주·특수종목 제외)
_VALID_CODE = re.compile(r'^\d{6}$')

# 세션 내 yfinance 조회 실패 종목 캐시 (당일 재시도 방지)
_failed_tickers: set = set()


def is_valid_code(symbol: str) -> bool:
    """yfinance로 조회 가능한 정규 종목코드 여부"""
    return bool(_VALID_CODE.match(symbol))


class MarketDataKOSPI:
    """한국 코스피 시장 데이터 조회 (항상 실전 API 사용)"""

    def __init__(self, kis_client):
        self.client = kis_client

    def get_bulk_ohlcv_yf(self, symbols: list, kospi_set: set = None, lookback: int = 100) -> dict:
        """
        yfinance 일괄 조회 — 2771번 요청 → 1번 요청.
        kospi_set: KOSPI 종목 집합 (나머지는 KOSDAQ → .KQ 접미사)
        반환: {symbol: DataFrame(date,open,high,low,close,volume)}
        """
        kospi_set = kospi_set or set(symbols)

        # 비정상 코드(우선주·특수종목) 및 이미 실패한 종목 사전 제거
        filtered = [s for s in symbols if is_valid_code(s) and s not in _failed_tickers]
        skipped  = len(symbols) - len(filtered)
        if skipped:
            logger.debug(f"  ⏭ 비정상/실패 종목 {skipped}개 제외 (우선주·특수종목·상장폐지)")

        if not filtered:
            return {}

        yf_map = {
            f"{s}.KS" if s in kospi_set else f"{s}.KQ": s
            for s in filtered
        }
        yf_tickers = list(yf_map.keys())

        logger.info(f"  📡 yfinance {len(yf_tickers)}개 일괄 다운로드 중...")
        raw = yf.download(
            yf_tickers,
            period='6mo',
            auto_adjust=True,
            progress=False,
        )

        result = {}
        for yf_sym, orig_sym in yf_map.items():
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    stock_df = pd.DataFrame({
                        'open':   raw[('Open',   yf_sym)],
                        'high':   raw[('High',   yf_sym)],
                        'low':    raw[('Low',    yf_sym)],
                        'close':  raw[('Close',  yf_sym)],
                        'volume': raw[('Volume', yf_sym)],
                    })
                else:
                    stock_df = raw[['Open','High','Low','Close','Volume']].rename(
                        columns=str.lower)

                stock_df = stock_df.dropna(how='all').tail(lookback).copy()

                if len(stock_df) < 20:
                    _failed_tickers.add(orig_sym)   # 실패 캐시 등록
                    continue

                stock_df = stock_df.reset_index()
                stock_df['date'] = pd.to_datetime(stock_df['Date']).dt.strftime('%Y%m%d')
                result[orig_sym] = stock_df[['date', 'open', 'high', 'low', 'close', 'volume']]
            except Exception:
                _failed_tickers.add(orig_sym)       # 실패 캐시 등록
                continue

        logger.info(f"  ✅ {len(result)}/{len(filtered)}개 수신 완료")
        return result

    def get_kospi_ohlcv(self, symbol, interval='1d', lookback=100):
        """OHLCV 조회 — KIS 기간별시세(100일) 우선, pykrx 폴백"""
        try:
            if interval == '1d':
                # 1. KIS API (FHKST03010100) — 날짜 범위 지정, 최대 100거래일
                rows = self.client.get_kr_daily_ohlcv(symbol, lookback=lookback)
                if rows and len(rows) >= 60:   # SMA_60 계산 가능한 충분한 데이터
                    df = pd.DataFrame(rows)
                    df['date'] = df['date'].astype(str)
                    return df[['date', 'open', 'high', 'low', 'close', 'volume']].tail(lookback)

                # 2. yfinance 폴백 — KIS API 데이터 부족 시 (.KS -> .KQ 순 시도)
                for suffix in [".KS", ".KQ"]:
                    try:
                        df_yf = yf.download(
                            f"{symbol}{suffix}", period="6mo",
                            auto_adjust=True, progress=False,
                        )
                        if isinstance(df_yf.columns, pd.MultiIndex):
                            df_yf.columns = df_yf.columns.get_level_values(0)
                        df_yf = df_yf.dropna(how="all")
                        if len(df_yf) < 20:
                            continue
                        df_yf = df_yf.reset_index()
                        df_yf["date"] = pd.to_datetime(df_yf["Date"]).dt.strftime("%Y%m%d")
                        df_yf = df_yf.rename(columns={
                            "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume",
                        })
                        return df_yf[["date","open","high","low","close","volume"]].tail(lookback)
                    except Exception:
                        continue

                return None

            else:
                # 분봉/실시간 데이터는 KIS 실전 API 사용
                url = f"{self.client.data_base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
                params = {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_HOUR_1": datetime.now().strftime("%H%M%S"),
                    "FID_PW_DATA_INCU_YN": "Y",
                    "FID_ETC_CLS_CODE": ""
                }
                result = {}
                for auth_attempt in range(2):
                    headers = {
                        "content-type": "application/json; charset=utf-8",
                        "authorization": f"Bearer {self.client.data_token}",
                        "appkey": self.client.data_appkey,
                        "appsecret": self.client.data_appsecret,
                        "tr_id": "FHKST03010200",
                        "custtype": "P"
                    }
                    response = requests.get(url, headers=headers, params=params, timeout=10)
                    result = response.json()
                    if result.get('rt_cd') == '0':
                        break
                    if result.get('msg_cd') == 'EGW00123' and auth_attempt == 0:
                        logger.warning("⚠️ 분봉 조회 data_token 만료 — 재발급 재시도")
                        self.client._delete_cached_token(self.client.data_appkey)
                        self.client.data_token = self.client._get_token(
                            self.client.data_base_url,
                            self.client.data_appkey,
                            self.client.data_appsecret,
                        )
                        continue
                    logger.warning(
                        f"⚠️ {symbol} 분봉 조회 실패: "
                        f"{result.get('msg1') or response.text[:120]}"
                    )
                    return None
                rows = result.get('output2') or []
                parsed = []
                for row in rows:
                    close = row.get('stck_prpr') or row.get('stck_prpr'.upper())
                    if not close:
                        continue
                    parsed.append({
                        'date': row.get('stck_bsop_date', ''),
                        'time': row.get('stck_cntg_hour', ''),
                        'open': float(row.get('stck_oprc', close) or close),
                        'high': float(row.get('stck_hgpr', close) or close),
                        'low': float(row.get('stck_lwpr', close) or close),
                        'close': float(close),
                        'volume': float(row.get('cntg_vol', 0) or 0),
                    })
                if not parsed:
                    return None
                df = pd.DataFrame(parsed)
                return df.sort_values(['date', 'time']).tail(lookback)
        except Exception as e:
            logger.error(f"❌ {symbol} 시세 조회 실패: {e}")
            return None

    def get_current_price(self, symbol: str) -> float | None:
        """KIS API 현재가 단건 조회"""
        if not self.client.data_token:
            return None
        url = f"{self.client.data_base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = {
            "content-type" : "application/json",
            "authorization": f"Bearer {self.client.data_token}",
            "appkey"       : self.client.data_appkey,
            "appsecret"    : self.client.data_appsecret,
            "tr_id"        : "FHKST01010100",
            "custtype"     : "P",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD"        : symbol,
        }
        try:
            res  = requests.get(url, headers=headers, params=params, timeout=5)
            data = res.json()
            if data.get('rt_cd') == '0':
                return float(data['output']['stck_prpr'])
            return None
        except Exception as e:
            logger.debug(f"현재가 조회 실패 ({symbol}): {e}")
            return None

    def get_investor_trading_flow(self, symbol, date):
        """투자자별 매매동향 (실전 API 사용)"""
        url = f"{self.client.data_base_url}/uapi/domestic-stock/v1/quotations/investor-trading-flow"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.client.data_token}",
            "appkey": self.client.data_appkey,
            "appsecret": self.client.data_appsecret,
            "tr_id": "FHKST01010900",
            "custtype": "P"
        }
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if data.get('rt_cd') == '0':
                out = data['output']
                return {
                    'foreigner_net': int(out['frgn_ntby_qty']),
                    'institution_net': int(out['orgn_ntby_qty']),
                    'net_flow': int(out['frgn_ntby_qty']) + int(out['orgn_ntby_qty'])
                }
            return None
        except: return None

    def get_kospi_constituents(self):
        """코스피/코스닥 대표 종목 (하드코딩 백업 — main 흐름 미사용)"""
        return [
            {"symbol": "005930", "name": "삼성전자"},
            {"symbol": "000660", "name": "SK하이닉스"},
            {"symbol": "005380", "name": "현대차"},
            {"symbol": "000270", "name": "기아"},
            {"symbol": "068270", "name": "셀트리온"},
            {"symbol": "035420", "name": "NAVER"},
            {"symbol": "086520", "name": "에코프로"},
            {"symbol": "247540", "name": "에코프로비엠"},
        ]
