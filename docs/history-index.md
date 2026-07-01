# History Index

Last updated: 2026-07-01

This file is the quick lookup index for logs, migrations, AI review history, and performance artifacts.

## 1. Local workspace history

### Account migration archives
- [`data/account_migrations/20260701_172750_pre_kiwoom_mock_switch.json`](/Users/hoisung/Downloads/kospi_trading_system/data/account_migrations/20260701_172750_pre_kiwoom_mock_switch.json)
  - local switch archive before Kiwoom mock state reset

### Local portfolio snapshots
- [`paper_trading_logs/portfolio_20260701.json`](/Users/hoisung/Downloads/kospi_trading_system/paper_trading_logs/portfolio_20260701.json)
  - current local paper portfolio state

### Local runtime logs
- [`logs/kospi_trading.log`](/Users/hoisung/Downloads/kospi_trading_system/logs/kospi_trading.log)
- [`logs/screening.log`](/Users/hoisung/Downloads/kospi_trading_system/logs/screening.log)
- [`logs/stderr.log`](/Users/hoisung/Downloads/kospi_trading_system/logs/stderr.log)

### Local architecture / audit notes
- [`docs/handoff/`](/Users/hoisung/Downloads/kospi_trading_system/docs/handoff)
  - historical audit and handoff documents
- [`docs/kr-bot-saas-plan.md`](/Users/hoisung/Downloads/kospi_trading_system/docs/kr-bot-saas-plan.md)

## 2. Production server history

Server root:
- `/home/ubuntu/kospi_box_bot`

### Primary runtime log
- `/home/ubuntu/kospi_box_bot/logs/runner.log`

### Daily AI review history
- `/home/ubuntu/kospi_box_bot/journal/20260623_ai_review.md`
- `/home/ubuntu/kospi_box_bot/journal/20260624_ai_review.md`
- `/home/ubuntu/kospi_box_bot/journal/20260625_ai_review.md`
- `/home/ubuntu/kospi_box_bot/journal/20260626_ai_review.md`
- `/home/ubuntu/kospi_box_bot/journal/20260627_ai_review.md`
- `/home/ubuntu/kospi_box_bot/journal/20260628_ai_review.md`
- `/home/ubuntu/kospi_box_bot/journal/20260629_ai_review.md`
- `/home/ubuntu/kospi_box_bot/journal/20260630_ai_review.md`
- `/home/ubuntu/kospi_box_bot/journal/20260701_ai_review.md`

### Daily AI change history
- `/home/ubuntu/kospi_box_bot/journal/20260623_ai_changes.json`
- `/home/ubuntu/kospi_box_bot/journal/20260624_ai_changes.json`
- `/home/ubuntu/kospi_box_bot/journal/20260625_ai_changes.json`
- `/home/ubuntu/kospi_box_bot/journal/20260626_ai_changes.json`
- `/home/ubuntu/kospi_box_bot/journal/20260627_ai_changes.json`
- `/home/ubuntu/kospi_box_bot/journal/20260628_ai_changes.json`
- `/home/ubuntu/kospi_box_bot/journal/20260629_ai_changes.json`
- `/home/ubuntu/kospi_box_bot/journal/20260630_ai_changes.json`
- `/home/ubuntu/kospi_box_bot/journal/20260701_ai_changes.json`

### Strategy evolution
- `/home/ubuntu/kospi_box_bot/journal/strategy_evolution_board.md`
- `/home/ubuntu/kospi_box_bot/journal/strategy_evolution_ledger.json`
- `/home/ubuntu/kospi_box_bot/journal/active_strategy_profile.json`

### Runtime/account state
- `/home/ubuntu/kospi_box_bot/data/paper_state.json`
- `/home/ubuntu/kospi_box_bot/data/realtime_runtime.json`
- `/home/ubuntu/kospi_box_bot/data/kiwoom_mock_account_state.json`
- `/home/ubuntu/kospi_box_bot/data/kiwoom_token_cache.json`

### Server account migration archives
- `/home/ubuntu/kospi_box_bot/data/account_migrations/20260701_172337_pre_kiwoom_migration.json`

### Pattern/performance datasets
- `/home/ubuntu/kospi_box_bot/data/patterns/clusters.json`
- `/home/ubuntu/kospi_box_bot/data/patterns/clusters_k16.json`
- `/home/ubuntu/kospi_box_bot/data/patterns/exit_analysis.json`
- `/home/ubuntu/kospi_box_bot/data/patterns/surge_patterns.jsonl`
- `/home/ubuntu/kospi_box_bot/data/bt_arm_sweep_result.txt`
- `/home/ubuntu/kospi_box_bot/data/bt_cache/`

## 3. Fast retrieval guide

If you need:

- current production runtime error:
  - `/home/ubuntu/kospi_box_bot/logs/runner.log`
- current mock account state:
  - `/home/ubuntu/kospi_box_bot/data/kiwoom_mock_account_state.json`
- why strategy changed on a specific date:
  - matching `/home/ubuntu/kospi_box_bot/journal/YYYYMMDD_ai_review.md`
  - matching `/home/ubuntu/kospi_box_bot/journal/YYYYMMDD_ai_changes.json`
- migration/reset history:
  - local `data/account_migrations/`
  - server `data/account_migrations/`
- architecture reasoning:
  - `docs/system-map.md`
  - `docs/operations-runbook.md`

## 4. Rule for future history

Any future operational change should leave one or more of:
- a dated review markdown
- a dated change JSON
- a migration archive JSON
- a log note in the runbook if operationally significant

This keeps changes searchable by date instead of only by memory.
