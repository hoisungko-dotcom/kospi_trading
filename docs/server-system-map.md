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

## High-value files
- `realtime/daily_runner.py`
- `realtime/realtime_runtime.py`
- `realtime/kiwoom_realtime.py`
- `realtime/kiwoom_mock_broker.py`
- `collector/kiwoom_client.py`
- `data/kiwoom_mock_account_state.json`
- `data/kiwoom_token_cache.json`

## Current broker mode
- `.env`
  - `BOX_BOT_BROKER=kiwoom_mock`

## Generated indexes
- `docs/history-index.md`
- `docs/history-index.json`
