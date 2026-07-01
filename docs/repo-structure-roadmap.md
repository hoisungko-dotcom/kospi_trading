# Repo Structure Roadmap

Last updated: 2026-07-01

## Goal

Move from a historically-grown bot repo to a structure that looks like a broker-agnostic trading SaaS.

## Target top-level presentation

- `brokers/`
- `runtime/`
- `services/`
- `state/`
- `strategy/`
- `docs/`

## Mapping from current layout

### Current `core/`

Split by responsibility:
- broker implementations
  - move toward `brokers/`
- strategy logic
  - move toward `strategy/`
- account/report/sync helpers
  - move toward `services/`

### Current `kospi_bot_v2/`

This is already closer to a SaaS-friendly shape.

Subfolders map roughly as:
- `kospi_bot_v2/runtime/` -> `runtime/`
- `kospi_bot_v2/strategy/` -> `strategy/`
- `kospi_bot_v2/risk/` -> `strategy/` or `services/` depending on final ownership
- `kospi_bot_v2/reporting/` -> `services/`
- `kospi_bot_v2/domain/` -> `state/`
- `kospi_bot_v2/market/` -> split between `brokers/` and shared `services/`

## Migration phases

### Phase 1. Naming standardization

Status:
- in progress

Definition:
- common code uses role names
- broker names live only in adapter layers

### Phase 2. Visible structure scaffolding

Status:
- started

Definition:
- top-level SaaS-oriented folders exist
- docs explain what belongs where

### Phase 3. Import-safe relocation

Definition:
- move modules behind compatibility shims
- keep old imports working during transition

Examples:
- old import:
  - `from core.kis_client_kospi import KISClientKospi`
- new import target later:
  - `from brokers.kis.client import KISClientKospi`

### Phase 4. Legacy collapse

Definition:
- remove temporary aliases
- remove duplicate entrypoints
- make one runtime path clearly primary

## Rules during migration

- no large file moves without compatibility wrappers
- no broker-brand naming in common runtime files
- document the new location before moving the old one
- prefer additive transitions over destructive refactors

## Recommended next moves

1. Create import-compatible package shims for `brokers/`, `runtime/`, `services/`, `state/`, `strategy/`.
2. Move one vertical slice first:
   - broker adapters
   - broker profile/factory
   - quote providers
3. Then move one runtime slice:
   - v2 live runner
   - v2 shadow runner
   - market-hours helpers
4. Only after that, reduce `core/`.

## Current completed slices

- broker slice
  - canonical implementations now live under `brokers/`
  - legacy `core/*` imports are kept as wrappers where needed
  - v2 broker market helpers now also point to `brokers/` canonical files
- runtime slice
  - canonical live runner: `runtime/live_runner.py`
  - canonical shadow runner: `runtime/shadow_runner.py`
  - canonical market-hours helper: `runtime/market_hours.py`
  - `kospi_bot_v2/runtime/*` remains as compatibility wrappers
