# Production Server Map

Server root: `/home/ubuntu/kospi_box_bot`

## Runtime
- service: `kospi_box_bot.service`
- start command:
  - `/home/ubuntu/kospi_box_bot/venv/bin/python -m realtime.daily_runner --delay 0.15`
- main log:
  - `/home/ubuntu/kospi_box_bot/logs/runner.log`

## Core folders
- `realtime/`
  - live box bot runtime
- `collector/`
  - Kiwoom REST and token helpers
- `data/`
  - runtime/account state, migrations, pattern data
- `journal/`
  - daily AI review and change history
- `review/`
  - review outputs
- `docs/`
  - server-side maps and generated indexes

## Active runtime files
- `realtime/daily_runner.py`
- `realtime/realtime_runtime.py`
- `realtime/kiwoom_realtime.py`
- `realtime/kis_mock_broker.py`
- `collector/kiwoom_client.py`

## Runtime state files
- `data/live_strategy_state.json`
- `data/paper_state.json`
- `data/kiwoom_token_cache.json`
- `data/paper_state.json`
- `data/realtime_runtime.json`

## Current broker mode
- `.env`
  - `KIS_TRADING_MODE=live|mock`

## Current runtime truth

- Live runtime is `BoxChecker v2` only
- Live strategy state persists to `data/live_strategy_state.json`
- `data/paper_state.json` is legacy/paper-mode state and should not be treated as live strategy truth
- `realtime/strategy_state_engine.py` is the neutral entrypoint for runtime strategy state
- `realtime/kis_broker.py` is the neutral entrypoint for runtime KIS broker access
- `realtime/paper_engine.py` and `realtime/kis_mock_broker.py` remain as compatibility layers

## Legacy and backup artifacts
- `realtime/*.bak_*`
  - historical backup files from prior KIS/hybrid stages
  - useful for archaeology, but not part of the active runtime path

## Naming rule
- Active runtime logs and new code should prefer `broker`, `market data`, `account`, `execution` wording.
- KIS/Kiwoom names should stay inside broker-specific implementations or legacy backups.

## Generated indexes
- `docs/history-index.md`
- `docs/history-index.json`
