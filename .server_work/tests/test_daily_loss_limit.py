from pathlib import Path

from realtime.daily_runner import _daily_buy_block_reason, _rebuild_symbol_daily_state
from realtime.paper_engine import PaperEngine


def _engine_with_tmp_state(tmp_path: Path) -> PaperEngine:
    return PaperEngine(path=tmp_path / "paper_state.json")


def test_daily_loss_limit_blocks_new_buys(tmp_path):
    engine = _engine_with_tmp_state(tmp_path)
    engine.buy("000270", "기아", 100000, "20260702093000")
    engine.sell("000270", 96000, "20260702093100", "trailing_stop")
    engine.buy("005930", "삼성전자", 100000, "20260702094000")
    engine.sell("005930", 96000, "20260702094100", "trailing_stop")
    engine.buy("000660", "SK하이닉스", 100000, "20260702095000")
    engine.sell("000660", 96000, "20260702095100", "trailing_stop")

    _rebuild_symbol_daily_state(engine, "20260702")
    reason = _daily_buy_block_reason("20260702")
    assert reason.startswith("daily_loss_limit:")


def test_daily_trade_limit_counts_attempts(tmp_path):
    engine = _engine_with_tmp_state(tmp_path)
    for idx, code in enumerate(["000270", "005930", "000660", "035420", "068270", "012330", "024110", "015760", "055550", "005490"]):
        ts = f"2026070209{30 + idx:02d}00"
        engine.buy(code, code, 100000, ts)
        engine.sell(code, 101000, f"2026070209{31 + idx:02d}00", "trailing_stop")

    _rebuild_symbol_daily_state(engine, "20260702")
    reason = _daily_buy_block_reason("20260702")
    assert reason.startswith("daily_trade_limit:")
