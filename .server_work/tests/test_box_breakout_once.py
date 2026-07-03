import realtime.daily_runner as dr


def test_box_breakout_attempt_once_per_day():
    dr._BOX_BREAKOUT_ATTEMPTS.clear()
    assert dr._box_breakout_attempted("000270", 100.0, 98.0, 25, "A", "20260702") is False
    dr._record_box_breakout_attempt("000270", 100.0, 98.0, 25, "A", "20260702")
    assert dr._box_breakout_attempted("000270", 100.0, 98.0, 25, "A", "20260702") is True
    assert dr._box_breakout_attempted("000270", 100.0, 98.0, 25, "A", "20260703") is False
