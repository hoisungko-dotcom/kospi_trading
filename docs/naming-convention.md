# Naming Convention

Last updated: 2026-07-01

## Goal

This project should look like a broker-agnostic trading SaaS, not a one-off bot with mixed historical names.

Professional appearance comes from:
- stable directory boundaries
- role-based naming
- consistent env keys
- consistent runtime logs
- explicit separation between common code and broker-specific code

## Core rule

Use role names in common code.

Allowed in common layers:
- `broker`
- `market_data`
- `account`
- `execution`
- `flow`
- `realtime`
- `universe`
- `review`
- `history`
- `runtime`
- `state`

Do not use broker brand names in common layers:
- `kis`
- `kiwoom`
- any other future broker name

Broker names are allowed only inside:
- broker adapter implementations
- broker-specific env compatibility fallbacks
- historical migration notes
- legacy compatibility wrappers

## Naming zones

### 1. Common runtime and strategy layer

Use:
- `broker_client`
- `market_data_client`
- `account_client`
- `execution_client`
- `flow_client`
- `broker_sync`
- `broker_profile`
- `broker_factory`
- `broker_direct_universe`

Avoid:
- `kis_client`
- `kiwoom_client` in strategy/runtime orchestration
- `kis_sync`
- `kis_direct_universe`

### 2. Broker-specific implementation layer

Use explicit broker names here because the code is intentionally vendor-specific.

Examples:
- `core/kis_client_kospi.py`
- `core/kiwoom_client_kospi.py`
- `realtime/kis_mock_broker.py`
- `realtime/kiwoom_mock_broker.py`

Rule:
- file names may contain broker names
- class names may contain broker names
- logs inside these files may mention the broker

### 3. Environment variables

Standard names going forward:
- `BROKER_PROFILE`
- `BROKER_SYNC_INTERVAL_SEC`
- `BROKER_AUTO_LIQUIDATE_EXCLUDED`
- `BROKER_EXCLUDED_RETRY_INTERVAL_SEC`
- `BOX_BOT_UNIVERSE_BROKER_MARKET`

Compatibility rule:
- old broker-specific env keys may still be read as fallback
- new code should write and document only the standard names first

Example:
- preferred: `BROKER_SYNC_INTERVAL_SEC`
- fallback allowed: `KIS_SYNC_INTERVAL_SEC`

## Logging rule

Common runtime logs must use role wording.

Preferred:
- `브로커 재동기화 완료`
- `브로커 매수가능수량 0`
- `브로커 모의매수 실패`
- `브로커 유니버스`

Avoid in common runtime logs:
- `KIS 재동기화`
- `KIS 매수가능수량`
- `KIS 유니버스`

Broker-specific logs may still say:
- `KIS 토큰 발급`
- `Kiwoom 현재가 실패`

## Directory presentation rule

Professional SaaS-facing structure should trend toward:
- `runtime/`
- `brokers/`
- `strategy/`
- `services/`
- `state/`
- `docs/`

Current local repo is not fully migrated yet, but all new additions should move toward this presentation.

## Documentation rule

Every structural document should distinguish:
- common layer
- broker-specific layer
- legacy compatibility layer
- production runtime layer

Required documents:
- `docs/system-map.md`
- `docs/operations-runbook.md`
- `docs/history-index.md`
- `docs/naming-convention.md`

## Current migration status

### Already standardized
- `main.py` broker capability naming
- `core/account_balance_reporter.py`
- `core/broker_interfaces.py`
- `core/broker_profile.py`
- `core/broker_factory.py`
- most common runtime log wording on server

### Still intentionally broker-specific
- `core/kis_client_kospi.py`
- `core/kiwoom_client_kospi.py`
- `core/api_client.py`
- `realtime/kis_mock_broker.py`
- `realtime/kis_realtime.py`
- `realtime/kiwoom_mock_broker.py`
- `realtime/kiwoom_realtime.py`

### Still candidates for future cleanup
- `kospi_bot_v2/` KIS naming in quote/live broker modules
- old docs and handoff files that describe historical KIS-only designs
- local/server filenames that still expose broker names in state/cache files

## Review checklist

Before merging future changes, check:

1. Does common code use a broker brand name?
2. Does a new env key use broker-specific wording where a role name is possible?
3. Do runtime logs use role wording?
4. Is a legacy compatibility alias clearly marked as legacy?
5. Can a new AI find the right file from the name alone?

If any answer is no, rename before expanding functionality.
