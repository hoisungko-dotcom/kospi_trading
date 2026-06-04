# Codex Review - KR Shadow Strategy League Design

Date: 2026-06-04

## Decision

Conditionally approved.

The architecture, independent portfolios, immutable strategy versions, cash benchmark, and daily/weekly reports are good.

Do not deploy until the corrections below are implemented and tested.

## Required Corrections

### 1. A-v1 must be a true control

The proposed A-v1 says:

```text
current bot behavior, DEFENSE_LONG disabled
exit: -4% / +8%, trailing +5%
```

This is not the current bot behavior. Current deployed defaults are:

```text
stop: -2%
take: +5%
trailing start: +2.5%
trailing gap: 1%
```

Define:

```text
A-v1 = exact currently deployed behavior, DEFENSE_LONG disabled
A-v2 = proposed -4%/+8%/trail+5% experiment
```

Without a true control, the league cannot tell whether changes improve the current bot.

### 2. Strategies must evaluate raw market frames independently

Do not make B/C/D depend only on the existing live runner's generated `signals`.

If `league.on_signals(existing_signals)` is the only input, the league can test only variants of A's candidate logic. It cannot fairly discover candidates rejected by A.

Preferred interface:

```python
league.evaluate(
    frame=frame,
    regime=regime,
    prices=prices,
    timestamp=snapshot.timestamp,
)
```

Each strategy definition must independently evaluate the same raw frame.

### 3. D-v1 requires sufficient lookback

Current `KISQuoteOnlyProvider` default lookback is 100.

SMA224 cannot be calculated reliably from 100 rows.

Before enabling D-v1:

- increase shadow-league data lookback to at least 260-300
- verify KIS data retrieval actually returns enough rows
- log and skip D-v1 when SMA224 is unavailable
- never interpret missing SMA224 as zero or a passed condition

Do not increase the live runner's API load blindly. A separate cached daily-data refresh for the shadow league is acceptable.

### 4. Exit evaluation must run during market hours

The design mentions intraday signal evaluation and daily finalization, but exits also need evaluation.

For every strategy portfolio:

- evaluate exits on every live loop or defined evaluation interval
- update mark-to-market NAV
- persist open positions safely
- do not wait until 16:00 to notice a stop/trailing condition

Document whether shadow fills use:

```text
current observed price
next loop price
next-day open
```

Use one consistent, conservative execution model.

### 5. Daily entry limit requires deterministic ordering

When several candidates qualify simultaneously, define a stable ranking:

```text
strategy score descending
then liquidity/trading value descending
then symbol ascending as final tie-break
```

Otherwise the one-entry-per-day constraint may produce non-reproducible results.

### 6. C-v1 should test the breakout hypothesis rather than exclude it prematurely

Generic BREAKOUT performed poorly, but the league is shadow-only and exists to compare hypotheses.

Use:

```text
C-v1 = breakout in all regimes, with regime recorded
C-v2 = exceptional breakout outside BULL with stricter filters
```

Do not exclude CRASH completely from C-v1. Analyze results by regime. The sample will remain small, but the league should collect evidence rather than hard-code the conclusion.

### 7. B-v1 BULL-only is approved

B-v1 is the cleanest test of the current hypothesis:

```text
confirmed uptrend pullback only in BULL
```

Keep it BULL-only.

Create future B-v2 rather than mutating B-v1 if testing other regimes.

### 8. Portfolio constraints are approved with one clarification

Approved:

```text
initial capital: KRW 2,000,000 per strategy
max positions: 2
daily entries: 1
round-trip cost: 0.35%
minimum allocation: KRW 100,000
```

The total KRW 10M is purely virtual and is fine.

Report both:

- return percentage, primary comparison metric
- NAV in KRW, readability metric

### 9. Reports need data-quality and comparability fields

Add:

```text
evaluation loops completed / expected
missing-price count
skipped signals due to missing indicators
strategy active/inactive reason
number of eligible candidates before daily-entry limit
cash utilization
```

Fix the sample weekly inconsistency:

```text
B shows 3 trades, but conclusion says 10 trading days / 0 trades.
```

### 10. Timer syntax and timezone must be verified

Do not assume:

```text
OnCalendar=Mon-Fri 16:00 KST
```

is valid systemd syntax.

Use `systemd-analyze calendar` on AWS to verify the exact expression and timezone before deployment.

The timers must not run on KRX holidays. The report job may run and state "KRX holiday/no session", but it must not count that date as a trading day.

## Strategy Definitions After Corrections

```text
A-v1 Exact current deployed control, DEFENSE_LONG disabled
A-v2 Fixed exit experiment: -4%/+8%, trail start +5%
B-v1 BULL-only confirmed-trend pullback
C-v1 Breakout all regimes, results segmented by regime
C-v2 Exceptional breakout outside BULL, stricter filter
D-v1 B-v1 plus valid SMA224 gate
E-v1 Cash/no trade
```

Do not silently change these definitions after deployment.

## Live Safety Requirements

Keep:

```text
V2_NEW_ENTRIES_ENABLED=false
```

Shadow league must:

- never import or call live-order methods
- never write live portfolio state
- use its own state directory
- survive restart without duplicate shadow fills
- use atomic state writes

## Required Tests Before Deployment

In addition to the existing 33 tests, add tests proving:

1. A-v1 exactly matches current deployed settings.
2. B/C/D evaluate the raw frame independently of A signals.
3. D skips safely when SMA224 is unavailable.
4. Exit evaluation occurs intraday.
5. Candidate ordering is deterministic.
6. Restart does not duplicate a shadow trade.
7. State writes are atomic/recoverable.
8. Timer expressions are validated on AWS.
9. KRX holidays do not increment trading-day counters.
10. The live broker/order API cannot be reached from shadow code.

## Approval Boundary

After these corrections and tests:

- approve deployment of the shadow league and timers
- keep real new entries disabled
- begin the 60-trading-day league schedule

Report the corrected strategy definitions, timer validation output, test results, and a final sample report before deployment.

