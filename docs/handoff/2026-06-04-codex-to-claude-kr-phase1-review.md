# Codex Review - KR Swing Phase 1 Results

Date: 2026-06-04

## Decision

Do not deploy the proposed Phase 2 strategy yet.

All tested strategies still have profit factor below 1.0 after costs:

```text
Current DEFENSE_LONG: PF 0.95
SMA224 gate:         PF 0.97
Trend+RelStr:        PF 0.88
Best hold variant:   PF 0.98
```

Changing live parameters from `-2%/+5%` to `-4%/+8%` would reduce stop-outs, but the tested strategy still loses money. Lower stop frequency is not sufficient evidence of positive expectancy.

The user wants profit-first trading. Do not deploy a strategy merely because it loses less.

## Approved Immediate Safety Change

`DEFENSE_LONG` may be disabled for new live entries because:

- It contradicts the intended swing philosophy.
- It has negative expectancy.
- It repeatedly enters short rebounds inside broader downtrends.

Disabling it is a prevention measure, not a claim that the replacement strategy is ready.

Do not deploy the wider stop/target or new PULLBACK logic yet.

## Required Phase 1.5 Before Live Phase 2

### 1. Verify the backtest itself

The claim that KOSPI daily ATR is generally 6-8% appears unusually high for many liquid stocks.

Report:

- median ATR percentage
- 25th/75th percentile ATR percentage
- ATR distribution by KOSPI/KOSDAQ
- ATR distribution by candidate strategy

Check for data, adjustment, or calculation errors.

### 2. Test RS thresholds instead of assuming RS >= 0 will improve

Compare:

```text
RS20 >= -5%
RS20 >= -2%
RS20 >= 0%
RS20 >= +0.5%
RS20 percentile top 50%
RS20 percentile top 30%
```

Relative-strength percentile ranking is preferred over a fixed absolute threshold because market conditions change.

Also test combined 20/60-day ranking:

```text
0.6 * RS20 percentile + 0.4 * RS60 percentile
```

### 3. Backtest the actual redesigned morning scanner

The root problem is the candidate scanner. Testing entry rules on the existing large-cap-biased universe does not validate the intended strategy.

Build and compare:

```text
current morning scanner
broad liquid universe + strength ranking
broad liquid universe + strength ranking + controlled pullback
```

Universe should include sufficiently liquid KOSPI/KOSDAQ stocks, not only top market-cap names.

Use trading value/liquidity filters to avoid illiquid names.

### 4. Separate market regimes and subperiods

Report performance separately for:

```text
BULL
NEUTRAL
WEAK
CRASH
```

Also report yearly/subperiod results.

The desired strategy may profit in BULL/NEUTRAL and correctly stay mostly inactive in WEAK/CRASH. Overall PF below 1 can hide a profitable conditional strategy.

### 5. Test entry confirmation quality

The target setup is:

```text
strong trend
controlled pullback
renewed acceleration
```

Compare entries:

```text
next-day open
close regains SMA5/SMA20
breaks previous day's high
volume expansion after pullback
intraday confirmation proxy if available
```

Buying immediately after detecting a pullback may still catch a falling stock. Require evidence that the pullback is ending.

### 6. Compare exit policies on the improved entry set

Only after scanner/entry improvements, compare:

```text
-4% / +8%
-5% / +10%
ATR-aware
5-day hold
10-day hold
```

Do not select exit parameters using the losing old entry set.

## Success Criteria Before Live Deployment

Recommended minimum:

```text
Profit factor > 1.10 after 0.35% costs
positive average net PnL
positive results across more than one subperiod
no single symbol or small group dominates profits
reasonable trade count
max drawdown explicitly reported
```

If no tested strategy meets this threshold, the correct action is no live entry, not deployment of a less-negative strategy.

## Implementation Order

1. Optionally disable `DEFENSE_LONG` live entries and test.
2. Verify ATR calculations.
3. Build the strength-ranked broad-universe scanner.
4. Test RS percentile thresholds and renewed-acceleration entry.
5. Run regime/subperiod analysis.
6. Test exit policies on the improved candidate/entry set.
7. Report results before any live strategy deployment.

## User-Facing Conclusion

The current Phase 1 report successfully proves what not to do:

- Do not keep `DEFENSE_LONG`.
- Do not use `-2%` stop for this swing concept.
- Do not use a 45-minute time stop.
- Do not rely on SMA224 alone.

It has not yet proven a profitable replacement strategy.

The next task is to find positive expectancy, not merely reduce losses.

