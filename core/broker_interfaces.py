from __future__ import annotations

from typing import Protocol


class MarketDataProvider(Protocol):
    def get_bulk_daily_ohlcv(self, symbols: list[str], kospi_set: set | None = None) -> list[dict]: ...
    def get_current_prices(self, symbols: list[str]) -> dict[str, float]: ...
    def get_daily_ohlcv(self, symbol: str) -> dict | None: ...
    def get_intraday_ohlcv(self, symbol: str, interval: str = "1m", lookback: int = 30): ...
    def get_top_trading_value_symbols(self, limit: int = 200, market: str = "001") -> list[dict]: ...
    def get_top_net_buying_symbols(self, limit: int = 200, market: str = "001") -> list[dict]: ...


class AccountProvider(Protocol):
    def get_balance(self) -> dict: ...
    def get_orderable_cash(self, symbol: str, price: float, use_max: bool = False) -> float: ...
    def verify_domestic_fill(
        self,
        symbol: str,
        side: str,
        previous_qty: int,
        order_qty: int,
        retries: int | None = None,
        delay_sec: float | None = None,
    ) -> bool | str: ...


class ExecutionProvider(Protocol):
    def place_buy_order(
        self,
        symbol: str,
        quantity: int,
        price: float,
        allow_price_chase: bool = False,
        market_order: bool = False,
    ) -> bool: ...
    def place_sell_order(self, symbol: str, quantity: int, price: float, market_order: bool = False) -> bool: ...


class FlowProvider(Protocol):
    def get_foreign_net_buying(self, symbol: str, lookback: int = 5) -> list[dict]: ...


class BrokerClient(MarketDataProvider, AccountProvider, ExecutionProvider, FlowProvider, Protocol):
    is_mock: bool
    broker_name: str
