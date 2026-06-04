# Codex Decision After KR Swing Phase 1.5

Date: 2026-06-04

## Decision

Proceed with Option A and Option C in parallel.

Do not deploy Option B yet.

Approved immediate live safety change:

```text
Disable new DEFENSE_LONG entries.
```

Do not yet deploy:

```text
SMA224 gate as the final strategy
-5% / +10% exits
BULL position-size increase
```

## Why Option B Is Not Yet Approved

The recent result is promising but insufficient:

```text
2026 PF: 1.15
BULL PF: 1.04
Two-year best overall PF: below 1.00
CRASH PF 2.22: only 50 trades
```

Risks that must be resolved:

1. 2026 is a partial year and may reflect a favorable short regime.
2. Using today's top KOSPI/KOSDAQ constituents for historical testing can create survivorship bias.
3. Summed trade returns such as `+185%` are not portfolio returns and must not be treated as account performance.
4. CRASH PF 2.22 with 50 trades is a hypothesis, not proof.
5. The live bot currently does not properly persist a multi-day entry timestamp for KIS-rebuilt positions, so a 5/10-day time exit cannot be relied on yet.

## Option A - Extend And Audit Backtest

Extend to at least 2018-2026 if data permits, including:

```text
2018 downturn
2020 crash/rebound
2021 bull period
2022 bear period
2023 recovery
2024 weakness
2025-2026 recent period
```

Required audit:

- Confirm no lookahead in regime classification, indicators, candidate ranking, or entry prices.
- State how same-day stop and take collisions are resolved.
- Include gap-through-stop handling and realistic execution assumptions.
- Avoid survivorship bias where possible. If historical constituents are unavailable, disclose the limitation clearly.
- Report portfolio-level performance with position limits and capital constraints, not only independent summed trades.
- Report CAGR, max drawdown, Sharpe/Sortino if available, profit factor, trade count, and yearly returns.
- Report results after 0.35% costs.

## Option C - Isolate Breakout Strategy

Backtest BREAKOUT separately from PULLBACK/DEFENSE_LONG.

The CRASH result suggests that a small number of exceptionally strong stocks may work even in weak markets. Test the hypothesis, but do not assume it is true.

Compare:

```text
BREAKOUT in BULL
BREAKOUT in NEUTRAL
BREAKOUT in WEAK
BREAKOUT in CRASH
```

Test the user's breakout checklist as optional filters:

- strong breakout candle body
- close near candle high / limited upper wick
- meaningful breakout volume
- first pullback only
- support zone hold
- pullback volume contraction

Do not require next-day close confirmation or previous-high breakout by default because Phase 1.5 showed those confirmations damaged performance. Test each filter independently and in combinations.

## Additional Tests Requested

### 1. Regime policy

Compare:

```text
BULL only
BULL + exceptional BREAKOUT in non-BULL regimes
all regimes with regime-adjusted sizing
```

The likely final design is:

```text
PULLBACK only in BULL
exceptional BREAKOUT allowed outside BULL with stricter score/size
```

### 2. SMA224 role

SMA224 gate improved results but did not create positive expectancy.

Test it as:

```text
hard gate
score bonus
falling-SMA224 rejection only
SMA60 slope + SMA224 combination
```

### 3. Exit policy

Re-test exits on BREAKOUT and PULLBACK separately. They likely require different exits.

Suggested comparison:

```text
PULLBACK: -4%/+8%, -5%/+10%, ATR-aware
BREAKOUT: wider trailing exit, no early hard take, failed-breakout exit
```

### 4. Portfolio simulation

Apply actual live constraints:

- available cash
- maximum positions
- position sizing
- no overlapping use of the same capital
- transaction costs
- realistic execution at next open

## Success Criteria For Deployment

Before deploying a replacement strategy:

```text
Portfolio-level PF >= 1.10 after costs
Positive expectancy across multiple years/regimes
Acceptable max drawdown
No single year/symbol dominates total profit
Enough trades for confidence
No material lookahead/survivorship flaw
```

## Required Next Report

Report:

1. Extended-period audited results.
2. Portfolio-level rather than summed-trade results.
3. BREAKOUT-only results by regime.
4. PULLBACK-only results by regime.
5. Results of optional breakout-quality filters.
6. Recommended final live strategy and exact deployment changes.

