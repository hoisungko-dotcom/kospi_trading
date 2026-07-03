from collections import deque

from realtime.box_checker import BoxChecker
from realtime.kiwoom_realtime import RealtimeQuoteState
from realtime.realtime_strategy import BoxRealtimeState, RealtimeEntryConfirmer


class DummyCandle:
    def __init__(self, open_, high, low, close, volume):
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def test_box_grade_a_and_c():
    checker = BoxChecker()
    a_candles = [DummyCandle(100, 101, 99.8, 100.2, 1000 - i * 20) for i in range(40)]
    c_candles = [DummyCandle(100, 103, 99, 102, 2000) for _ in range(10)]
    assert checker._grade_box(a_candles, 1.2) == "A"
    assert checker._grade_box(c_candles, 4.0) == "C"


def test_realtime_confirm_requires_all_guards_before_weak_breakout_check():
    confirmer = RealtimeEntryConfirmer()
    state = BoxRealtimeState(
        code="000270",
        name="기아",
        box_high=100.0,
        box_low=98.0,
        preferred=True,
        daily_pass=True,
        box_height_pct=1.0,
        box_length=25,
        box_grade="A",
        avg_box_volume=1000.0,
    )
    quote = RealtimeQuoteState(
        code="000270",
        last_price=100.25,
        best_bid=100.2,
        best_ask=100.25,
        bid_ask_imbalance=0.25,
        trade_velocity=6,
        cum_volume_delta=2200,
        recent_prices=deque([100.0, 100.1, 100.25], maxlen=12),
    )
    ok, _ = confirmer.confirm_entry(state, quote)
    assert ok is True

    weak_quote = RealtimeQuoteState(
        code="000270",
        last_price=100.05,
        best_bid=100.0,
        best_ask=100.05,
        bid_ask_imbalance=-0.05,
        trade_velocity=6,
        cum_volume_delta=900,
        recent_prices=deque([100.0, 100.01, 100.05], maxlen=12),
    )
    ok, reason = confirmer.confirm_entry(state, weak_quote)
    assert ok is False
    assert reason == "low_trade_volume"
