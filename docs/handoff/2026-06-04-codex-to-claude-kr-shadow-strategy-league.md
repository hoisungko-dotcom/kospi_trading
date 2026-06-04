# Codex to Claude Handoff - KR Shadow Strategy League

Date: 2026-06-04

## Goal

Keep KR live new-entry kill switch active while running multiple strategies in parallel as shadow portfolios.

The user must be able to understand which strategy is working without checking the bot every day.

## Core Rule

Every strategy must receive:

- the same market data
- the same candidate universe
- the same evaluation timestamps
- the same initial capital
- the same transaction-cost assumptions
- independent cash and position state

Never mix strategy results into one portfolio. Do not let one strategy's trade block another strategy's shadow trade.

## Initial Strategy League

Run at least:

```text
A: Control
   Current strategy behavior, but DEFENSE_LONG disabled.

B: Trend Pullback
   Confirmed upward trend, controlled pullback, no general weak-market entries.

C: Breakout
   Strong breakout candidate, tested separately from pullback.

D: SMA224 Gate
   Simple long-trend gate benchmark.

E: Cash
   No trades. Benchmark proving whether trading adds value.
```

Additional strategies may be added, but never silently change an existing strategy definition. Version strategy rules:

```text
B-v1, B-v2, C-v1...
```

## Shadow Portfolio Constraints

Use realistic constraints from the start:

```text
initial capital: same value for every strategy
maximum positions: 2 initially
daily new entries: maximum 1
transaction costs: 0.35% round trip or actual configured cost
no overlapping use of the same strategy's capital
realistic next-open/live shadow execution
```

Record:

- signal time
- intended execution price
- actual shadow execution price
- exit price/reason
- strategy version
- regime
- score and rejection reasons
- MFE/MAE
- holding time

## Schedule

### Every Market Day

```text
08:30-09:00  Build/refresh broad candidate universe
09:00-15:30  Evaluate every strategy on the same schedule
15:40        Mark portfolios to market and finalize daily metrics
16:00        Send one concise daily report
```

Daily report should show:

```text
strategy NAV
daily PnL
cumulative PnL
open positions
new entries/exits
largest missed/rejected opportunity
errors or data-quality issues
```

Do not send repetitive alerts when no meaningful change occurred.

### Every Friday After Market Close

Generate a weekly league table:

```text
rank
strategy/version
trades
win rate
PF
average net PnL
NAV return
MDD
stop rate
average MFE/MAE
average holding days
performance by regime
```

Include plain-Korean conclusions:

```text
이번 주 가장 좋았던 전략
성과가 나빴던 이유
표본 부족 여부
다음 주에도 유지할 전략
변경이 필요한 전략
```

### Evaluation Gates

```text
10 trading days: operational/data-quality review only
20 trading days or 30 closed trades: first performance review
40 trading days or 50 closed trades: promotion/rejection review
60 trading days: final initial league review
```

Do not promote a strategy solely because it ranks first. It must beat the cash benchmark and satisfy minimum quality.

## Promotion Criteria For Small Live Exploration

Minimum recommendation threshold:

```text
PF >= 1.10 after costs
positive portfolio NAV return
MDD within user-approved range
positive results across multiple weeks
not dominated by one symbol/trade
no unresolved execution/data bugs
minimum 30 closed trades, preferably 50
```

If no strategy passes, keep live entries disabled.

## User Decision Report

The user should not need to inspect raw logs.

Create a single latest summary file and Telegram summary:

```text
KR 전략 리그 현황

1위: B-v1 Trend Pullback
NAV: +2.1%
PF: 1.24
MDD: -1.8%
거래: 18건
판단: 표본 부족, 유지 관찰

2위: E Cash
NAV: 0.0%

3위: C-v1 Breakout
NAV: -1.2%
판단: 거래량 돌파 필터 재검토 필요

실전 전환 가능 전략: 없음
다음 평가일: YYYY-MM-DD
```

Always include the cash/no-trade benchmark.

## Automation Requirements

Implement systemd timers or an equivalent reliable scheduler for:

- candidate refresh
- daily finalization/report
- weekly league report

The existing `kr-bot.service` can continue intraday evaluation. Keep:

```text
V2_NEW_ENTRIES_ENABLED=false
```

Shadow league must never call the live broker's real `buy()`/`sell()` path.

Add a clear shadow-only guard and tests.

## Required Tests

1. Every strategy receives identical input timestamps/universe.
2. Strategy portfolios maintain independent cash/positions.
3. Shadow league cannot call live order APIs.
4. Costs apply consistently.
5. Daily/weekly reports calculate NAV, PF, MDD correctly.
6. Strategy versions remain immutable after creation.
7. Cash benchmark remains unchanged.
8. Missing data is reported and does not silently become a trade.

## Deliverables Before Deployment

Report:

- architecture and files to add/change
- exact strategy definitions
- scheduler/timer plan
- sample daily report
- sample weekly league report
- tests
- confirmation that real new entries remain disabled

Do not deploy until user approves the report format and strategy definitions.

