# Operations Runbook

Last updated: 2026-07-01

## 1. Production target

- Host alias: `aws-trading`
- Host path: `/home/ubuntu/kospi_box_bot`
- Service: `kospi_box_bot.service`

Service definition:
- WorkingDirectory: `/home/ubuntu/kospi_box_bot`
- ExecStart:
  - `/home/ubuntu/kospi_box_bot/venv/bin/python -m realtime.daily_runner --delay 0.15`
- Log file:
  - `/home/ubuntu/kospi_box_bot/logs/runner.log`

## 2. Current broker mode

Current server `.env` indicates:
- `BOX_BOT_BROKER=kiwoom_mock`

Meaning:
- runtime broker/account path is Kiwoom mock
- real-time path is Kiwoom based

## 3. Safe inspection commands

Check service:

```bash
ssh aws-trading 'systemctl is-active kospi_box_bot.service'
```

Check service definition:

```bash
ssh aws-trading 'systemctl cat kospi_box_bot.service'
```

Check recent service logs:

```bash
ssh aws-trading 'journalctl -u kospi_box_bot.service --since "30 minutes ago" --no-pager -n 200'
```

Check runtime log tail:

```bash
ssh aws-trading 'tail -n 120 /home/ubuntu/kospi_box_bot/logs/runner.log'
```

Check broker mode:

```bash
ssh aws-trading 'cd /home/ubuntu/kospi_box_bot && grep -nE "^(BOX_BOT_BROKER|BROKER_|KIWOOM_|KIS_)" .env'
```

## 4. Files to patch first for common issues

### Token / REST 429 / auth problems
- `/home/ubuntu/kospi_box_bot/collector/kiwoom_client.py`

### Runtime real-time subscription issues
- `/home/ubuntu/kospi_box_bot/realtime/kiwoom_realtime.py`

### Order and balance shadow-state issues
- `/home/ubuntu/kospi_box_bot/realtime/kiwoom_mock_broker.py`
- `/home/ubuntu/kospi_box_bot/data/kiwoom_mock_account_state.json`

### Universe loading issues
- `/home/ubuntu/kospi_box_bot/realtime/universe_loader.py`
- `/home/ubuntu/kospi_box_bot/collector/kiwoom_client.py`

### Main runtime orchestration
- `/home/ubuntu/kospi_box_bot/realtime/daily_runner.py`

## 5. Restart procedure

Compile first:

```bash
ssh aws-trading 'cd /home/ubuntu/kospi_box_bot && ./venv/bin/python -m py_compile collector/kiwoom_client.py realtime/daily_runner.py realtime/kiwoom_realtime.py realtime/kiwoom_mock_broker.py'
```

Restart:

```bash
ssh aws-trading 'sudo systemctl restart kospi_box_bot.service'
```

Verify:

```bash
ssh aws-trading 'systemctl is-active kospi_box_bot.service'
ssh aws-trading 'tail -n 80 /home/ubuntu/kospi_box_bot/logs/runner.log'
```

## 6. Current known important runtime files

- token cache:
  - `/home/ubuntu/kospi_box_bot/data/kiwoom_token_cache.json`
- account state:
  - `/home/ubuntu/kospi_box_bot/data/kiwoom_mock_account_state.json`
- engine state:
  - `/home/ubuntu/kospi_box_bot/data/paper_state.json`
- runtime state:
  - `/home/ubuntu/kospi_box_bot/data/realtime_runtime.json`

## 7. Deployment warning

This repo and the production server runtime are not the same codebase.

## 8. Naming policy

Operationally, prefer these standard env names first:
- `BROKER_SYNC_INTERVAL_SEC`
- `BROKER_AUTO_LIQUIDATE_EXCLUDED`
- `BROKER_EXCLUDED_RETRY_INTERVAL_SEC`
- `BOX_BOT_UNIVERSE_BROKER_MARKET`

Legacy broker-specific env names may still exist for compatibility:
- `KIS_SYNC_INTERVAL_SEC`
- `KIS_AUTO_LIQUIDATE_EXCLUDED`
- `KIS_EXCLUDED_RETRY_INTERVAL_SEC`
- `BOX_BOT_UNIVERSE_KIS_MARKET`

Always state one of:
- "local workspace only"
- "production server runtime"

before claiming a change is deployed.
