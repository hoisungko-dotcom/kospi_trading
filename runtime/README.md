# Runtime

Purpose:
- orchestration entrypoints
- scheduling
- market-hour handling
- live/shadow runners

Canonical runner files:
- `runtime/live_runner.py`
- `runtime/shadow_runner.py`
- `runtime/market_hours.py`

Target contents over time:
- active runtime entrypoints
- process orchestration
- service-facing runner code

Current source migration candidates:
- `main.py`
- `kospi_bot_v2/main.py`
- `kospi_bot_v2/runtime/`
