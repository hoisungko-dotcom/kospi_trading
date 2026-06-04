# Codex Decision - Freeze KR Live Entries And Audit Backtest

Date: 2026-06-04

## Decision

Freeze all new KR live entries.

Keep the KR service running only for:

- balance/account monitoring
- logs
- diagnostics
- research/shadow signals

Do not place new live buy orders until a portfolio-level strategy passes the deployment criteria.

Existing positions, if any, must continue to be monitored and exited safely under the currently deployed exit logic. Do not abandon an open position merely because new entries are frozen.

## Why

The tested strategies failed the user's profit-first requirement:

```text
Best portfolio PF: 0.93
Most portfolio MDD: about -88%
Most strategies depleted capital within several years
BREAKOUT and PULLBACK both had negative expectancy
No tested combination achieved PF >= 1.10
```

No-entry is better than deploying a proven negative-expectancy strategy.

## Important Backtest Audit Warning

Do not accept the portfolio report blindly yet.

The following results are suspicious:

```text
Different strategies ended near the same NAV: about KRW 591K-600K
Different strategies produced nearly identical CAGR: about -22.3%
Different strategies produced nearly identical MDD: about -88%
S2 stopped trading after cash fell below about KRW 600K
```

This may be real, but it may also indicate a shared simulator constraint or bug.

Also, this explanation is incomplete:

```text
"Portfolio PF collapsed because compounding reduced capital."
```

Proportional compounding changes NAV and drawdown, but profit factor should not automatically collapse from 0.96 to 0.54 solely because capital shrinks. A large PF change can result from:

- concurrency/position selection changing which trades execute
- position size varying over time
- cash constraints skipping later winners
- minimum trade size/floor effects
- incorrect cash accounting
- overlapping trades using or blocking capital incorrectly
- fees applied incorrectly
- positions not closed or marked correctly

Audit the simulator before using the exact PF/NAV values for final strategic conclusions.

## Immediate Implementation

Implement a safe live-entry kill switch for KR bot.

Preferred:

```text
V2_NEW_ENTRIES_ENABLED=false
```

Behavior:

- Existing exits continue.
- Balance synchronization continues.
- Candidate generation and diagnostics may continue.
- New live buy orders are blocked with a clear log message.
- Shadow/research runs remain possible.

Do not stop `kr-bot.service` entirely unless the user explicitly asks.

Add tests proving:

1. New entries are blocked when kill switch is false.
2. Existing position exits still execute.
3. Diagnostics/signals can still run.

## Backtest Engine Audit

Create deterministic small-case tests covering:

1. One winning trade and one losing trade with known expected NAV/PF.
2. Overlapping signals with maximum-position constraints.
3. Cash reserved and released correctly.
4. Fees charged exactly once on intended legs.
5. Gap-through-stop execution.
6. Same-day stop/take collision policy.
7. Final open positions marked to market or closed consistently.
8. Minimum trade amount behavior.
9. Position sizing after gains/losses.
10. No future data used in signal/regime calculation.

Report both:

```text
trade-level PF
portfolio cash-flow PF
```

Explain precisely why they differ.

## Strategy Direction After Audit

Do not continue endlessly tuning the same indicator mixture.

The tests show that:

- generic PULLBACK is negative
- generic BREAKOUT is negative
- candle-quality filters do not rescue BREAKOUT
- simple market regime filters do not rescue the strategy

If the audit confirms these results, redesign around a different source of edge.

Research candidates:

1. Event/catalyst swing:
   - earnings surprise
   - guidance/contract/order/news catalyst
   - abnormal trading value
   - gap that holds rather than generic technical breakout

2. Cross-sectional momentum rotation:
   - periodically hold only the strongest small set
   - replace losers/weakening names
   - use portfolio-level ranking, not thousands of independent entry signals

3. Sector leadership rotation:
   - identify leading sectors first
   - trade strongest stocks inside leading sectors

4. Overnight/close-to-open behavior:
   - test whether the actual edge is overnight rather than intraday swing

Each must be tested as a portfolio from the start.

## Deployment Criteria

No new live strategy until:

```text
Audited portfolio PF >= 1.10 after costs
Positive CAGR
MDD acceptable to user
Positive across multiple periods
No single year/symbol dominates profit
No known lookahead/survivorship flaw
```

## Required Report

Report back:

- kill-switch implementation status
- tests
- whether existing exits remain active
- backtest audit findings
- corrected portfolio results
- recommendation: repair existing strategy or replace it

