from __future__ import annotations

from datetime import datetime
import logging
import time

import pandas as pd

from brokers.kis.api_client import KISClient
from services.market_data import MarketDataKOSPI
from kospi_bot_v2.domain.models import MarketSnapshot
from kospi_bot_v2.market.data_provider import MarketDataProvider
from kospi_bot_v2.market.indicators import add_indicators


logger = logging.getLogger(__name__)


NAME_FALLBACKS = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "005380": "현대차",
    "000270": "기아",
    "068270": "셀트리온",
    "035420": "NAVER",
    "005490": "POSCO홀딩스",
    "042660": "한화오션",
    "196170": "알테오젠",
    "247540": "에코프로비엠",
    "028300": "HLB",
    "086520": "에코프로",
    "252670": "KODEX 200선물인버스2X",
    "251340": "KODEX 코스닥150선물인버스",
    "114800": "KODEX 인버스",
    "069500": "KODEX 200",
    "229200": "KODEX 코스닥150",
}

REGIME_PROXY_SYMBOLS = {
    "KSPI": ("069500", "KODEX 200"),
    "KDQ": ("229200", "KODEX 코스닥150"),
}


class KISQuoteOnlyProvider(MarketDataProvider):
    """KIS quote-only provider.

    It authenticates only the data endpoint and never imports or calls order
    execution code.
    """

    def __init__(self, symbols: tuple[str, ...], lookback: int = 100, request_interval: float = 0.12):
        self.symbols = symbols
        self.lookback = lookback
        self.request_interval = request_interval
        self.client = KISClient()
        self.client.data_token = self.client._get_token(
            self.client.data_base_url,
            self.client.data_appkey,
            self.client.data_appsecret,
        )
        if not self.client.data_token:
            raise RuntimeError("Broker quote token unavailable. Check broker data app credentials.")
        self.market_data = MarketDataKOSPI(self.client)

    def load_universe_frame(self) -> pd.DataFrame:
        rows = self._load_symbol_rows(self.symbols)
        if not rows:
            raise RuntimeError("No quote data loaded from broker/yfinance.")
        frame = add_indicators(pd.DataFrame(rows))

        index_frames = self._load_index_proxy_frames()
        if not index_frames:
            index_frames = [
                self._synthetic_index_rows(frame, "KSPI", "KOSPI"),
                self._synthetic_index_rows(frame, "KDQ", "KOSDAQ"),
            ]
        return pd.concat(index_frames + [frame], ignore_index=True)

    def _load_symbol_rows(
        self,
        symbols: tuple[str, ...],
        name_overrides: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        name_overrides = name_overrides or {}
        for symbol in symbols:
            df = self.market_data.get_kospi_ohlcv(symbol, interval="1d", lookback=self.lookback)
            if df is None or df.empty:
                logger.warning("No daily data for %s", symbol)
                continue
            current_price = self.market_data.get_current_price(symbol)
            if current_price:
                df = df.copy()
                last_idx = df.index[-1]
                df.loc[last_idx, "close"] = current_price
                df.loc[last_idx, "high"] = max(float(df.loc[last_idx, "high"]), current_price)
                df.loc[last_idx, "low"] = min(float(df.loc[last_idx, "low"]), current_price)
            name = name_overrides.get(symbol, self._name(symbol))
            for _, row in df.iterrows():
                timestamp = self._timestamp(row)
                rows.append(
                    {
                        "symbol": symbol,
                        "name": name,
                        "timestamp": timestamp.isoformat(),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                    }
                )
            time.sleep(self.request_interval)
        return rows

    def _load_index_proxy_frames(self) -> list[pd.DataFrame]:
        frames: list[pd.DataFrame] = []
        for index_symbol, (proxy_symbol, name) in REGIME_PROXY_SYMBOLS.items():
            rows = self._load_symbol_rows((proxy_symbol,), {proxy_symbol: name})
            if not rows:
                logger.warning("No regime proxy data for %s (%s)", index_symbol, proxy_symbol)
                continue
            proxy = pd.DataFrame(rows)
            proxy["symbol"] = index_symbol
            proxy["name"] = "KOSPI" if index_symbol == "KSPI" else "KOSDAQ"
            frames.append(add_indicators(proxy))
        return frames

    def market_snapshot(self, frame: pd.DataFrame) -> MarketSnapshot:
        from kospi_bot_v2.market.data_provider import CsvMarketDataProvider

        return CsvMarketDataProvider.__new__(CsvMarketDataProvider).market_snapshot(frame)

    def _name(self, symbol: str) -> str:
        return NAME_FALLBACKS.get(symbol, symbol)

    def _timestamp(self, row: pd.Series) -> datetime:
        raw = str(row.get("date", ""))
        if raw and raw != "nan":
            try:
                return pd.to_datetime(raw).to_pydatetime().replace(hour=9, minute=0, second=0, microsecond=0)
            except Exception:
                pass
        return datetime.now().replace(second=0, microsecond=0)

    def _synthetic_index_rows(self, frame: pd.DataFrame, symbol: str, name: str) -> pd.DataFrame:
        if symbol == "KSPI":
            source = frame[~frame["symbol"].isin({"196170", "247540", "028300", "086520"})]
        else:
            source = frame[frame["symbol"].isin({"196170", "247540", "028300", "086520"})]
        if source.empty:
            source = frame
        rows = (
            source.groupby("timestamp", as_index=False)
            .agg(open=("open", "mean"), high=("high", "mean"), low=("low", "mean"), close=("close", "mean"), volume=("volume", "sum"))
            .tail(self.lookback)
        )
        rows.insert(0, "name", name)
        rows.insert(0, "symbol", symbol)
        return add_indicators(rows)
