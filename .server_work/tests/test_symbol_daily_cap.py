from pathlib import Path

from realtime.daily_runner import (
    _daily_blacklist_codes,
    _rebuild_symbol_daily_state,
    _symbol_daily_block_reason,
)
from realtime.paper_engine import PaperEngine


def _engine_with_tmp_state(tmp_path: Path) -> PaperEngine:
    return PaperEngine(path=tmp_path / "paper_state.json")


def test_symbol_daily_cap_and_restart_restore(tmp_path):
    engine = _engine_with_tmp_state(tmp_path)
    engine.buy("042660", "한화오션", 100000, "20260702093000")
    engine.sell("042660", 99000, "20260702093100", "follow_through_fail")
    engine.buy("042660", "한화오션", 101000, "20260702094000")
    engine.sell("042660", 100000, "20260702094100", "trailing_stop")

    restored = PaperEngine(path=tmp_path / "paper_state.json")
    _rebuild_symbol_daily_state(restored, "20260702")

    assert _symbol_daily_block_reason("042660").startswith("symbol_daily_attempt_cap:")
    assert "042660" in _daily_blacklist_codes()


def test_symbol_daily_state_resets_by_date(tmp_path):
    engine = _engine_with_tmp_state(tmp_path)
    engine.buy("042660", "한화오션", 100000, "20260702093000")
    engine.sell("042660", 99000, "20260702093100", "follow_through_fail")

    _rebuild_symbol_daily_state(engine, "20260703")
    assert _symbol_daily_block_reason("042660") == ""
    assert _daily_blacklist_codes() == set()
