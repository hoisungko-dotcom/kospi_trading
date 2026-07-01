# Codex to Claude Handoff

Date: 2026-07-01
Repo: `/Users/hoisung/Downloads/kospi_trading_system`

## What Was Completed

The repo was refactored to look like a broker-agnostic trading SaaS instead of a mixed one-off bot repo.

Additional strategy decision:
- `Box Bot` is the only primary live strategy identity
- `main.py` is the current live strategy authority
- `kospi_bot_v2` should be treated as shadow/support/modular runtime, not a competing live strategy owner

Canonical layers are now:
- `brokers/`
- `runtime/`
- `services/`
- `state/`
- `strategy/`

Key completions:
- broker implementations moved into `brokers/`
- live and shadow runners standardized into `runtime/`
- market-data/order-execution/account helpers moved into `services/`
- position/account contracts moved into `state/`
- signal/risk/investor-flow logic moved into `strategy/`
- legacy `core/*` and `kospi_bot_v2/*` compatibility wrappers kept intact

## Most Important Current Files

### Canonical implementation paths

- `brokers/kis/api_client.py`
- `brokers/kis/client.py`
- `brokers/kis/quote_provider.py`
- `brokers/kiwoom/auth.py`
- `brokers/kiwoom/data_provider.py`
- `brokers/kiwoom/client.py`
- `runtime/live_runner.py`
- `runtime/shadow_runner.py`
- `runtime/market_hours.py`
- `services/account_balance.py`
- `services/market_data.py`
- `services/order_execution.py`
- `services/sector_monitor.py`
- `state/account_snapshot.py`
- `state/position_manager.py`
- `strategy/signal_analyzer.py`
- `strategy/risk.py`
- `strategy/investor_flow.py`

### Compatibility wrappers still intentionally present

- `core/api_client.py`
- `core/kis_client_kospi.py`
- `core/kiwoom_client_kospi.py`
- `core/account_balance_reporter.py`
- `core/market_data_kospi.py`
- `core/order_execution.py`
- `core/investor_flow.py`
- `core/position_manager.py`
- `core/sector_monitor.py`
- `core/risk_management.py`
- `core/signal_analyzer_kospi.py`
- `kospi_bot_v2/runtime/live_runner.py`
- `kospi_bot_v2/runtime/shadow_runner.py`
- `kospi_bot_v2/runtime/market_hours.py`
- `kospi_bot_v2/market/kis_quote_provider.py`
- `kospi_bot_v2/market/kiwoom_client.py`
- `kospi_bot_v2/market/kiwoom_data_provider.py`

## Current Behavioral State

Verified locally:
- `python -m py_compile ...` passed for the refactored canonical files
- `python -m kospi_bot_v2.main --sample` runs successfully
- `ShadowRunner` was restored and is no longer a missing import path

Observed sample run result shape:
- `KR shadow bot: regime=NEUTRAL, signals=1, equity=14,000,000 ...`

## Important Architecture Rules

1. New broker-specific code goes into `brokers/`.
2. New runner/orchestration code goes into `runtime/`.
3. New neutral support logic goes into `services/`, `state/`, or `strategy/`.
4. `core/*` should be treated as compatibility-first unless explicitly collapsing legacy code.
5. `Box Bot` is the single live strategy identity.
6. `kospi_bot_v2/shadow/*` still owns the shadow league domain logic.
7. Local workspace refactors do not automatically change the production server bot.

## Production Boundary

The active production runtime is separate:
- `/home/ubuntu/kospi_box_bot`
- service: `kospi_box_bot.service`

If asked to change actual running server behavior, you must work in the server repo/runtime separately.

## Recommended Read Order

1. [docs/architecture-blueprint.md](/Users/hoisung/Downloads/kospi_trading_system/docs/architecture-blueprint.md)
2. [docs/system-map.md](/Users/hoisung/Downloads/kospi_trading_system/docs/system-map.md)
3. [docs/naming-convention.md](/Users/hoisung/Downloads/kospi_trading_system/docs/naming-convention.md)
4. [docs/repo-structure-roadmap.md](/Users/hoisung/Downloads/kospi_trading_system/docs/repo-structure-roadmap.md)
5. [docs/operations-runbook.md](/Users/hoisung/Downloads/kospi_trading_system/docs/operations-runbook.md)

## If Claude Continues Refactoring

Good next tasks:
- reduce remaining real logic inside `core/` if safe
- merge useful `kospi_bot_v2` support features into Box Bot without reviving a second strategy identity
- optionally move more `kospi_bot_v2/reporting` or `kospi_bot_v2/domain` into canonical top-level layers
- normalize remaining docs that still describe older KIS-first assumptions
- create tests specifically for compatibility wrappers if wrapper churn continues

Avoid unless explicitly requested:
- deleting legacy wrappers aggressively
- changing production server runtime assumptions from this local repo alone
- renaming files in ways that break current legacy imports without wrappers

## Final State Summary

This repo is now in a stable “canonical layers + compatibility wrappers” state.

That means:
- the architecture is substantially cleaned up
- new contributors can find the right place to modify code
- old imports still mostly work
- the remaining work is incremental cleanup, not structural rescue
