from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from core.kis_client_kospi import KISClientKospi
from kospi_bot_v2.domain.models import Signal, SignalAction, StrategyType
from kospi_bot_v2.portfolio.live_broker import KISLiveBroker
from kospi_bot_v2.runtime.live_runner import LiveRunner


def test_verify_domestic_fill_returns_pending_when_balance_unavailable(monkeypatch):
    client = KISClientKospi.__new__(KISClientKospi)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "get_balance", lambda: {})

    status = client.verify_domestic_fill(
        "005930",
        "BUY",
        previous_qty=0,
        order_qty=1,
        retries=2,
        delay_sec=0,
    )

    assert status == "PENDING"


def test_live_broker_buy_skips_symbol_in_reentry_cooldown():
    broker = KISLiveBroker.__new__(KISLiveBroker)
    broker.positions = {}
    broker._sell_cooldowns = {"005930": time.time() + 60}

    signal = Signal(
        action=SignalAction.BUY,
        symbol="005930",
        name="005930",
        strategy=StrategyType.MOMENTUM,
        score=80,
        reason="test",
        price=70000,
    )

    assert broker.buy(signal, 1) is None


def test_minute_entry_confirm_allows_non_breakdown_pullback():
    class DummyClient:
        def get_intraday_ohlcv(self, _symbol, interval="1m", lookback=10):
            assert interval == "1m"
            return pd.DataFrame(
                {
                    "date": ["20260602"] * 6,
                    "time": ["090000", "090100", "090200", "090300", "090400", "090500"],
                    "open": [100, 101, 102, 101, 101, 102],
                    "high": [101, 102, 103, 102, 102, 103],
                    "low": [99, 100, 101, 100, 100, 101],
                    "close": [100, 101, 102, 101, 102, 102],
                    "volume": [1000, 1100, 1200, 1000, 900, 900],
                }
            )

    class DummyBroker:
        client = DummyClient()

    runner = LiveRunner.__new__(LiveRunner)
    runner.broker = DummyBroker()
    signal = Signal(
        action=SignalAction.BUY,
        symbol="005930",
        name="005930",
        strategy=StrategyType.MOMENTUM,
        score=80,
        reason="test",
        price=102,
        metadata={"evaluated_at": datetime.now()},
    )

    ok, reason = runner._minute_entry_confirm(signal)

    assert ok is True
    assert "1분봉 확인" in reason
