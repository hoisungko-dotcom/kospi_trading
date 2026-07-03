from types import SimpleNamespace

import realtime.daily_runner as dr


def test_watchlist_rebuild_excludes_daily_blacklist(monkeypatch):
    class DummyRtClient:
        def __init__(self):
            self._subs = set()

        def subscribed_codes(self):
            return list(self._subs)

        def unsubscribe(self, code):
            self._subs.discard(code)

        def subscribe(self, code):
            self._subs.add(code)

    monkeypatch.setattr(dr, "get_min_chart", lambda *args, **kwargs: [{"x": 1}] * 6)
    monkeypatch.setattr(dr, "parse_candle", lambda row: {"ts": "20260702093000", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000})
    monkeypatch.setattr(
        dr,
        "_watch_candidate_from_candles",
        lambda box_checker, candles, stk_cd, name: dr.BoxRealtimeState(
            code=stk_cd,
            name=name,
            box_high=101,
            box_low=99,
            preferred=True,
            daily_pass=True,
            box_height_pct=1.0,
            box_length=10,
        ),
    )
    monkeypatch.setattr(dr, "BOX_RT_WATCHLIST_MAX", 5)

    rt_client = DummyRtClient()
    stocks = [{"code": "042660", "name": "한화오션"}, {"code": "000270", "name": "기아"}]
    watchlist = dr._build_realtime_watchlist(stocks, SimpleNamespace(), rt_client, 0.0, {"042660"})

    assert "042660" not in watchlist
    assert "000270" in watchlist
