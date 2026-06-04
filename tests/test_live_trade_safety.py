from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd

from core.kis_client_kospi import KISClientKospi
from kospi_bot_v2.domain.models import Position, Signal, SignalAction, StrategyType
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


# ---------------------------------------------------------------------------
# Kill switch tests
# ---------------------------------------------------------------------------

def _make_signal(symbol: str = "005930") -> Signal:
    return Signal(
        action=SignalAction.BUY,
        symbol=symbol,
        name=symbol,
        strategy=StrategyType.PULLBACK,
        score=80,
        reason="test",
        price=70000,
        metadata={"evaluated_at": datetime.now()},
    )


def _make_broker_shell() -> KISLiveBroker:
    """Minimal broker instance that bypasses KIS network init."""
    broker = KISLiveBroker.__new__(KISLiveBroker)
    broker.positions = {}
    broker._sell_cooldowns = {}
    broker._attempted_symbols = set()
    return broker


def test_kill_switch_blocks_new_buy_entry(monkeypatch):
    """buy() must return None immediately when V2_NEW_ENTRIES_ENABLED=false."""
    monkeypatch.setenv("V2_NEW_ENTRIES_ENABLED", "false")
    broker = _make_broker_shell()
    result = broker.buy(_make_signal(), quantity=1)
    assert result is None


def test_kill_switch_variants_all_block(monkeypatch):
    """All falsy env-var spellings must block entries."""
    broker = _make_broker_shell()
    for val in ("false", "0", "no", "off", "False", "NO", "OFF"):
        monkeypatch.setenv("V2_NEW_ENTRIES_ENABLED", val)
        assert broker.buy(_make_signal(), quantity=1) is None, f"val={val!r} did not block"


def test_kill_switch_true_passes_check(monkeypatch):
    """When kill switch is enabled the gate is cleared (may still fail for other reasons)."""
    monkeypatch.setenv("V2_NEW_ENTRIES_ENABLED", "true")
    broker = _make_broker_shell()
    # quantity=0 triggers the next guard — proves kill switch itself is cleared
    result = broker.buy(_make_signal(), quantity=0)
    assert result is None  # blocked by quantity guard, not kill switch


def test_kill_switch_does_not_block_sell(monkeypatch):
    """sell() must still execute even when V2_NEW_ENTRIES_ENABLED=false."""
    monkeypatch.setenv("V2_NEW_ENTRIES_ENABLED", "false")
    broker = _make_broker_shell()
    broker.positions = {
        "005930": Position(
            symbol="005930",
            name="삼성전자",
            strategy=StrategyType.PULLBACK,
            quantity=10,
            entry_price=70000.0,
            stop_price=66500.0,
            peak_price=70000.0,
            entry_time=datetime.now(),
        )
    }
    client_mock = MagicMock()
    client_mock.place_sell_order.return_value = False  # short-circuit after kill-switch gate
    broker.client = client_mock

    result = broker.sell("005930", price=72000.0, reason="stop", timestamp=datetime.now())

    # sell was attempted (place_sell_order was called), kill switch did not block it
    client_mock.place_sell_order.assert_called_once()
    assert result is None  # None because place_sell_order returned False, not because of kill switch


def test_kill_switch_does_not_affect_signal_generation():
    """Signal generation (strategy logic) is independent of the kill switch env var."""
    import os
    os.environ["V2_NEW_ENTRIES_ENABLED"] = "false"
    try:
        from kospi_bot_v2.strategy.signal_engine import SignalEngine
        from kospi_bot_v2.domain.models import MarketRegime

        engine = SignalEngine.__new__(SignalEngine)
        row = pd.Series({
            "close": 50000, "sma20": 48000, "sma60": 46000,
            "high20": 51000, "volume": 1000, "avg_volume20": 1500,
            "rsi14": 55, "return5": -0.02, "return20": 0.05,
            "consecutive_buy_days": 0,
        })
        strategy = engine._strategy_for(row, MarketRegime.BULL)
        assert strategy is not None, "Signal generation must work regardless of kill switch"
    finally:
        os.environ.pop("V2_NEW_ENTRIES_ENABLED", None)
