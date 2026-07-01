# Box Bot Strategy Charter

Last updated: 2026-07-01

## Decision

This repo now treats **Box Bot** as the single primary trading strategy.

That means:
- the main live strategy authority is `main.py`
- the operational philosophy is Box Bot, not a separate v2 strategy identity
- `kospi_bot_v2` remains useful, but only as shadow / support / experimentation

## Strategy Identity

Primary name:
- `Box Bot`

External product label allowed:
- `한국봇`

Internal modular/runtime label allowed:
- `KR Bot`

Rule:
- `KR Bot` must not imply a separate live strategy philosophy
- it is only a runtime/module label unless explicitly promoted later

## Primary Trading Philosophy

Box Bot should remain:
- concentrated
- intraday-first
- fast-entry / fast-exit
- no overnight by default
- selective rather than broad multi-position accumulation

Operational picture:
1. morning universe scan
2. small candidate shortlist
3. short-term confirmation using intraday/tick-aware context when available
4. entry near box / early breakout / strong recovery inside the intended setup
5. fast profit-taking or failure exit
6. same-day cleanup preferred

## What Is Primary vs Secondary

### Primary live strategy path

- [main.py](/Users/hoisung/Downloads/kospi_trading_system/main.py)
- [strategy/signal_analyzer.py](/Users/hoisung/Downloads/kospi_trading_system/strategy/signal_analyzer.py)
- [strategy/risk.py](/Users/hoisung/Downloads/kospi_trading_system/strategy/risk.py)
- [state/position_manager.py](/Users/hoisung/Downloads/kospi_trading_system/state/position_manager.py)

This is the strategy that should define:
- entry timing
- exit timing
- profile switching
- position concentration
- intraday liquidation behavior

### Secondary support path

- [kospi_bot_v2](/Users/hoisung/Downloads/kospi_trading_system/kospi_bot_v2)

This path is not the primary strategy identity.
It is allowed to provide:
- shadow evaluation
- signal diagnostics
- regime gating
- structured reporting
- modular runner improvements

## What We Keep from `kospi_bot_v2`

Useful pieces to pull into Box Bot over time:
- regime-aware gating
- cleaner signal diagnostics
- shadow league / comparison tooling
- structured reports
- cleaner runtime separation

Already absorbed into Box Bot:
- standardized Box Bot reject codes in `strategy/signal_analyzer.py`
- `main.py` rejection logs now split `reject_code` and human-readable `reasons`

Useful examples:
- [kospi_bot_v2/strategy/signal_engine.py](/Users/hoisung/Downloads/kospi_trading_system/kospi_bot_v2/strategy/signal_engine.py)
- [kospi_bot_v2/shadow/league.py](/Users/hoisung/Downloads/kospi_trading_system/kospi_bot_v2/shadow/league.py)
- [runtime/shadow_runner.py](/Users/hoisung/Downloads/kospi_trading_system/runtime/shadow_runner.py)

## What We Do Not Want

We do not want:
- two separate live strategy identities
- duplicated entry logic drifting independently
- one engine called “main” and another engine silently acting as a second strategy owner

If logic is migrated from `kospi_bot_v2`, it should be absorbed into Box Bot, not kept as a rival philosophy.

## Current Interpretation of Existing Logic

Current `main.py` is not a perfectly pure “box only” implementation.
It currently mixes:
- breakout
- pullback
- momentum
- scalp/swing profile switching

But from the product and operator point of view, this still belongs under the Box Bot umbrella because it is the active concentrated live-trading engine.

## Architecture Rule Going Forward

1. Box Bot is the only primary live strategy.
2. `kospi_bot_v2` is shadow/support unless explicitly promoted later.
3. New live behavior should be justified as improving Box Bot.
4. If a v2 component is good, merge the component, not the competing identity.
