# KR Bot System Map

Last updated: 2026-07-01

## 1. What exists

This workspace contains two related but different systems.

### Local architecture/refactor workspace
- Root: `/Users/hoisung/Downloads/kospi_trading_system`
- Purpose:
  - strategy and architecture cleanup
  - broker abstraction work
  - documentation and future SaaS-oriented structure

### Actual production server bot
- Server root: `/home/ubuntu/kospi_box_bot`
- Service: `kospi_box_bot.service`
- Start command:
  - `/home/ubuntu/kospi_box_bot/venv/bin/python -m realtime.daily_runner --delay 0.15`
- Purpose:
  - currently running Korean box bot
  - real operational logs, runtime state, review journal

Do not assume changes in the local workspace automatically affect the server bot.

## 2. Top-level ownership

### Local workspace
- `main.py`
  - primary Box Bot live strategy entrypoint in the local workspace
  - still contains a large amount of live strategy logic
- `brokers/`
  - SaaS-facing target zone for broker-specific implementations
- `core/`
  - broker clients
  - account helpers
  - screeners
  - risk/position utilities
- `runtime/`
  - SaaS-facing target zone for orchestration entrypoints
  - current canonical runners now include live, shadow, and market-hours paths
- `services/`
  - SaaS-facing target zone for sync/report/universe helpers
- `state/`
  - SaaS-facing target zone for contracts and persistent state ownership
- `strategy/`
  - SaaS-facing target zone for canonical Box Bot strategy logic
- `kospi_bot_v2/`
  - newer modular runtime / shadow / support engine
  - not the primary live strategy identity
  - includes market, portfolio, runtime, strategy layers
- `docs/`
  - handoff notes, architecture notes, system map
- `data/`
  - local state, migrations, token caches
- `logs/`
  - local runtime logs
- `paper_trading_logs/`
  - local portfolio state snapshots

### Production server
- `realtime/`
  - actual box bot runtime
  - real operational strategy loop
- `collector/`
  - Kiwoom REST/token/chart client code
- `logs/runner.log`
  - primary runtime log
- `data/`
  - account state, runtime state, migration files, pattern data
- `journal/`
  - daily AI review markdown and change JSON
- `review/`
  - review outputs

## 3. Broker architecture

### Current local direction
The local workspace is being refactored toward Lego-block composition.
It is also being reshaped toward a SaaS-facing top-level structure:
- `brokers/`
- `runtime/`
- `services/`
- `state/`
- `strategy/`

Key files:
- [`core/broker_interfaces.py`](/Users/hoisung/Downloads/kospi_trading_system/core/broker_interfaces.py)
  - common broker capability interfaces
- [`core/broker_profile.py`](/Users/hoisung/Downloads/kospi_trading_system/core/broker_profile.py)
  - profile definitions: `kiwoom_full`, `kis_full`, `hybrid_safe`
- [`core/broker_factory.py`](/Users/hoisung/Downloads/kospi_trading_system/core/broker_factory.py)
  - chooses broker client and sector monitor from profile

### Current local broker implementations
- [`brokers/kiwoom/client.py`](/Users/hoisung/Downloads/kospi_trading_system/brokers/kiwoom/client.py)
  - Kiwoom-only adapter for:
    - daily data
    - minute data
    - tick data
    - balance
    - mock order execution
    - fill verification
- [`brokers/kis/client.py`](/Users/hoisung/Downloads/kospi_trading_system/brokers/kis/client.py)
  - KIS-specific order/balance adapter
- [`brokers/kis/api_client.py`](/Users/hoisung/Downloads/kospi_trading_system/brokers/kis/api_client.py)
  - KIS REST/auth client
- [`services/account_balance.py`](/Users/hoisung/Downloads/kospi_trading_system/services/account_balance.py)
  - broker-neutral account balance printer
- [`core/kis_balance_checker.py`](/Users/hoisung/Downloads/kospi_trading_system/core/kis_balance_checker.py)
  - legacy compatibility alias to the neutral balance reporter

### Common strategy-side modules
These files should stay broker-neutral as the refactor continues.

- [`core/market_data_kospi.py`](/Users/hoisung/Downloads/kospi_trading_system/core/market_data_kospi.py)
  - common market-data wrapper used by strategy and batch flows
- [`core/order_execution.py`](/Users/hoisung/Downloads/kospi_trading_system/core/order_execution.py)
  - generic order execution flow and retry handling
- [`core/position_manager.py`](/Users/hoisung/Downloads/kospi_trading_system/core/position_manager.py)
  - local portfolio state and broker sync guardrails
- [`core/sector_monitor.py`](/Users/hoisung/Downloads/kospi_trading_system/core/sector_monitor.py)
  - sector momentum scoring
  - currently implemented with KIS sector endpoints, but intended to sit behind the broker composition layer
- [`core/batch_processor.py`](/Users/hoisung/Downloads/kospi_trading_system/core/batch_processor.py)
  - nightly scan path built on the common market-data wrapper

### Current profile switch
- `.env`
  - `BROKER_PROFILE=kiwoom_full`

## 4. Local main entrypoint map

Primary file:
- [`main.py`](/Users/hoisung/Downloads/kospi_trading_system/main.py)

Current strategy interpretation:
- this is the active Box Bot strategy authority
- it is the file to read first when the question is “what is the real live strategy?”

Important objects in `KospiTopTenSystem`:
- `self.broker`
  - selected broker client from profile
- `self.market_data_client`
  - market data capability view
- `self.account_client`
  - balance and fill verification capability view
- `self.execution_client`
  - order execution capability view
- `self.flow_client`
  - foreign flow capability view
- `show_balance()`
  - uses the neutral account balance reporter instead of broker-specific output code

Important methods:
- `_sync_portfolio_from_broker`
  - broker account to local portfolio sync
- `_short_term_frame`
  - short-term frame selector
  - uses tick when profile market data is Kiwoom
- `realtime_monitoring`
  - main decision loop
- `execute_buy`
  - order sizing and buy flow
- `execute_sell`
  - sell execution flow
- `_log_rejection`
  - persists standardized `reject_code` plus readable reject text for Box Bot diagnostics

## 5. Where to change what

### Change broker composition
- local:
  - [`core/broker_profile.py`](/Users/hoisung/Downloads/kospi_trading_system/core/broker_profile.py)
  - [`core/broker_factory.py`](/Users/hoisung/Downloads/kospi_trading_system/core/broker_factory.py)

### Change Kiwoom local data/order behavior
- [`core/kiwoom_client_kospi.py`](/Users/hoisung/Downloads/kospi_trading_system/core/kiwoom_client_kospi.py)
- [`brokers/kiwoom/data_provider.py`](/Users/hoisung/Downloads/kospi_trading_system/brokers/kiwoom/data_provider.py)
- [`brokers/kiwoom/auth.py`](/Users/hoisung/Downloads/kospi_trading_system/brokers/kiwoom/auth.py)
- [`brokers/kiwoom/client.py`](/Users/hoisung/Downloads/kospi_trading_system/brokers/kiwoom/client.py)

### Change KIS quote-only runtime behavior
- [`brokers/kis/quote_provider.py`](/Users/hoisung/Downloads/kospi_trading_system/brokers/kis/quote_provider.py)

### Change local strategy logic
- [`main.py`](/Users/hoisung/Downloads/kospi_trading_system/main.py)
- `SignalAnalyzerKospi`, `DynamicExitAnalyzerKospi`, `RiskManagement` in `core/`

### Change shadow/support engine behavior
- [`kospi_bot_v2/main.py`](/Users/hoisung/Downloads/kospi_trading_system/kospi_bot_v2/main.py)
- [`runtime/live_runner.py`](/Users/hoisung/Downloads/kospi_trading_system/runtime/live_runner.py)
- [`runtime/shadow_runner.py`](/Users/hoisung/Downloads/kospi_trading_system/runtime/shadow_runner.py)
- [`kospi_bot_v2/shadow/league.py`](/Users/hoisung/Downloads/kospi_trading_system/kospi_bot_v2/shadow/league.py)

### Change common broker-neutral runtime helpers
- [`core/market_data_kospi.py`](/Users/hoisung/Downloads/kospi_trading_system/core/market_data_kospi.py)
- [`core/order_execution.py`](/Users/hoisung/Downloads/kospi_trading_system/core/order_execution.py)
- [`state/position_manager.py`](/Users/hoisung/Downloads/kospi_trading_system/state/position_manager.py)
- [`services/sector_monitor.py`](/Users/hoisung/Downloads/kospi_trading_system/services/sector_monitor.py)
- [`services/account_balance.py`](/Users/hoisung/Downloads/kospi_trading_system/services/account_balance.py)
- [`runtime/live_runner.py`](/Users/hoisung/Downloads/kospi_trading_system/runtime/live_runner.py)
- [`runtime/shadow_runner.py`](/Users/hoisung/Downloads/kospi_trading_system/runtime/shadow_runner.py)
- [`runtime/market_hours.py`](/Users/hoisung/Downloads/kospi_trading_system/runtime/market_hours.py)

## 5A. Compatibility boundary

The repo now has a clearer split:

- canonical implementation paths
  - `brokers/`
  - `runtime/`
  - `services/`
  - `state/`
  - `strategy/`
- legacy compatibility paths
  - `core/*`
  - `kospi_bot_v2/runtime/*`

Strategy identity rule:
- primary live strategy = Box Bot (`main.py`)
- `kospi_bot_v2` = shadow/support engine, not a competing live strategy owner

Compatibility files should stay thin wrappers only.
New logic should be added to canonical paths first.
## 6. Change production box bot runtime
- server:
  - `/home/ubuntu/kospi_box_bot/realtime/daily_runner.py`
  - `/home/ubuntu/kospi_box_bot/realtime/realtime_runtime.py`
  - `/home/ubuntu/kospi_box_bot/realtime/kiwoom_realtime.py`
  - `/home/ubuntu/kospi_box_bot/realtime/kiwoom_mock_broker.py`
  - `/home/ubuntu/kospi_box_bot/collector/kiwoom_client.py`

### Change production token or REST stability
- server:
  - `/home/ubuntu/kospi_box_bot/collector/kiwoom_client.py`

### Change production account state
- server:
  - `/home/ubuntu/kospi_box_bot/data/kiwoom_mock_account_state.json`

## 7. Runtime data and logs

### Local files
- local account migration archive:
  - [`data/account_migrations/20260701_172750_pre_kiwoom_mock_switch.json`](/Users/hoisung/Downloads/kospi_trading_system/data/account_migrations/20260701_172750_pre_kiwoom_mock_switch.json)
- local portfolio:
  - [`paper_trading_logs/portfolio_20260701.json`](/Users/hoisung/Downloads/kospi_trading_system/paper_trading_logs/portfolio_20260701.json)
- local token caches:
  - [`data/kiwoom_token_cache_mock.txt`](/Users/hoisung/Downloads/kospi_trading_system/data/kiwoom_token_cache_mock.txt)
  - [`data/.token_GOExFi1F.json`](/Users/hoisung/Downloads/kospi_trading_system/data/.token_GOExFi1F.json)
  - [`data/.token_WkgYdPNp.json`](/Users/hoisung/Downloads/kospi_trading_system/data/.token_WkgYdPNp.json)
- local logs:
  - [`logs/kospi_trading.log`](/Users/hoisung/Downloads/kospi_trading_system/logs/kospi_trading.log)
  - [`logs/screening.log`](/Users/hoisung/Downloads/kospi_trading_system/logs/screening.log)

### Server files
- service log:
  - `/home/ubuntu/kospi_box_bot/logs/runner.log`
- server account migration:
  - `/home/ubuntu/kospi_box_bot/data/account_migrations/20260701_172337_pre_kiwoom_migration.json`
- server runtime state:
  - `/home/ubuntu/kospi_box_bot/data/realtime_runtime.json`
- server paper engine state:
  - `/home/ubuntu/kospi_box_bot/data/paper_state.json`
- server token cache:
  - `/home/ubuntu/kospi_box_bot/data/kiwoom_token_cache.json`
- server daily reviews:
  - `/home/ubuntu/kospi_box_bot/journal/*_ai_review.md`
  - `/home/ubuntu/kospi_box_bot/journal/*_ai_changes.json`

## 7. Stability rules

These are the architecture rules going forward.

- Strategy code must not directly branch on KIS/Kiwoom except in broker factory/profile code.
- New broker-specific behavior belongs in adapter files, not in strategy logic.
- Runtime logs should use `broker`, `market data`, `account`, `execution` naming, not old broker-specific names.
- Production changes must always state whether they target:
  - local workspace, or
  - server runtime
- Any AI touching this repo should read:
  - this file
  - `docs/operations-runbook.md`
  - `docs/history-index.md`
  - `docs/naming-convention.md`
  - `docs/repo-structure-roadmap.md`
  before changing runtime behavior.
