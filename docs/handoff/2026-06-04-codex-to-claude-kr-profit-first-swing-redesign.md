# Codex to Claude Handoff - KR Profit-First Swing Redesign

Date: 2026-06-04

## Read First

Read the user's diagnosis:

```text
/Users/hoisung/Downloads/kospi_trading_system/docs/handoff/2026-06-04-kr-swing-strategy-redesign.md
```

This document refines the implementation direction. Do not blindly apply every parameter proposed in the original diagnosis.

## User's Non-Negotiable Intent

The KR bot should be an individual-stock, profit-first swing trader.

```text
시장 전체를 보고 우량주를 오래 보유하는 봇이 아니다.
개별 종목 중 실제로 강하게 상승하는 종목을 찾는다.
상승 추세 안에서 짧은 숨고르기/눌림에 진입한다.
짧은 스윙 수익을 실현한다.
시장 약세라도 강한 개별 종목은 거래할 수 있어야 한다.
방어보다 수익 기회를 우선한다.
```

The current behavior is wrong because it repeatedly buys short rebounds inside broader downtrends.

## Codex Judgment

The original redesign correctly identifies:

- `DEFENSE_LONG` is structurally misaligned and should be disabled.
- Candidate quality is the root problem.
- Fixed short-term signals are admitting falling knives.
- The current 45-minute holding concept is not swing trading.

However, do not implement these ideas blindly:

### Do not use only `close > sma224`

A strict SMA224-above-only rule can recreate large-cap/old-winner bias and exclude newly emerging strong trends.

The target is not simply "above the one-year moving average."

The target is:

```text
confirmed upward trend
positive medium-term slope
strong relative performance
orderly pullback
renewed demand/acceleration
```

### Do not immediately widen every live stop to -5%

Widening the stop before fixing candidate selection can turn frequent small losses into larger losses.

First fix entry quality and backtest exit policies. Deploy wider stops only after comparison results support them.

## Fundamental Strategy Rewrite

### 1. Remove DEFENSE_LONG from live entries

`DEFENSE_LONG` should not generate live buy signals.

Reason:

- It intentionally searches for longs in WEAK/CRASH conditions.
- It accepts weak RSI and short rebounds.
- Backtest evidence shows high stop frequency and negative expectancy.

Keep enum compatibility if needed, but `_strategy_for()` must not return `DEFENSE_LONG` for live entries.

### 2. Market regime becomes a risk modifier, not a stock-selection veto

The bot should prioritize individual-stock strength.

Market regime may adjust:

- position size
- maximum simultaneous positions
- minimum signal score

Market regime should not create a special weak-market long strategy.

Strong individual stocks may remain eligible in WEAK markets when their own trend/relative strength is exceptional.

CRASH may still block or heavily restrict new entries.

### 3. Replace large-cap preference with broad strength ranking

The live universe currently comes from:

```text
data/top_10_daily.json
legacy morning_screening()
```

Audit and redesign the morning scanner so ranking is based on strength, not size/name familiarity.

Candidate requirements should include:

- adequate liquidity/trading value
- positive 20-day and 60-day return
- positive SMA20 and SMA60 slopes
- close above SMA20 and preferably SMA60
- relative strength versus KOSPI/KOSDAQ over 20/60 days
- no late-stage vertical overextension
- sufficient but not collapsing volume

Avoid a hard market-cap-top-only universe. Use a broad liquid KOSPI/KOSDAQ universe.

Suggested ranking components:

```text
relative_strength_20
relative_strength_60
sma20_slope
sma60_slope
return20
return60
volume/trading-value quality
distance from recent high
pullback quality
```

### 4. Core entry pattern: strong trend plus controlled pullback

PULLBACK should become the primary live strategy.

Do not enter merely because five-day return is negative.

Require a confirmed trend first:

```text
return20 > 0
return60 > 0
sma20 slope > 0
sma60 slope > 0
close above sma60
relative strength versus market > 0
```

Then require a controlled pullback:

```text
return5 roughly -0.5% to -5%
close near/above sma20, or recovering it
not making a fresh 20-day low
volume contracts during pullback
entry-day/minute volume returns with price stabilization
```

The key distinction:

```text
buy a pause inside an uptrend
do not buy a bounce inside a downtrend
```

### 5. Keep selective breakout continuation

BREAKOUT may remain as a secondary strategy when:

- medium-term trend slopes are positive
- relative strength is high
- breakout volume is meaningful
- price is not excessively extended

Do not reject every strong stock just because it has no pullback.

### 6. Use slope and relative strength, not only SMA224

Add indicators required for trend context:

```text
sma20_prev / sma20_slope
sma60_prev / sma60_slope
sma120 or sma224 where available
return60
market_return20
market_return60
relative_strength20 = stock_return20 - market_return20
relative_strength60 = stock_return60 - market_return60
distance_from_high20 / high60
```

SMA224 may be used as a bonus or falling-knife blocker, but not as the only truth.

Important current issue:

```text
KISQuoteOnlyProvider default lookback is 100.
SMA224 cannot be computed with 100 rows.
```

If SMA224 is used, increase live lookback to at least 260-300 and verify KIS pagination/data support. Do not add an SMA224 hard gate while lookback remains 100.

### 7. Fix incomplete-day volume handling

Current live daily row replaces close with current price while today's volume may still be incomplete.

This can make strong candidates fail with `volume too weak`, especially early in the session.

Review whether volume gates should use:

- prior completed daily volume for daily trend selection
- intraday projected volume or time-adjusted volume for live confirmation

Do not compare early-session partial volume directly against full-day average volume without adjustment.

## Exit Philosophy

The bot is a short swing trader, not a 45-minute scalp bot.

### Preferred direction: volatility-aware exits

Backtest fixed exits and ATR-aware exits side by side.

Candidate policies:

```text
A. fixed: stop -3%, take +7%, trail start +4%, trail gap 2%
B. fixed: stop -4%, take +8%, trail start +5%, trail gap 2.5%
C. ATR-aware: stop 1.5 ATR, target at least 2R, trailing after 1R
D. original proposal: stop -5%, take +10%, trail start +5%, gap 3%
```

Use caps so extreme ATR stocks do not create absurd stops:

```text
minimum stop distance: about 2.5%
maximum stop distance: about 6%
```

The winner should be selected by:

- net expectancy after costs
- profit factor
- maximum drawdown
- stop-out rate
- average winner/loser
- number of trades

Do not optimize only for win rate.

### Holding period

Replace the 45-minute mindset with a day-based swing horizon.

Test:

```text
maximum hold 3, 5, and 10 trading days
```

A time exit should close positions that fail to move after several trading days, not after 45 minutes.

## Required Work Order

### Phase 1 - Analysis and backtest only

Do not deploy strategy changes yet.

1. Audit `morning_screening()` and explain why it selects current candidates.
2. Build a broad liquid universe backtest, not only KOSPI top 50.
3. Compare:
   - current strategy
   - original SMA224-only proposal
   - trend-slope + relative-strength pullback strategy
4. Compare exit policies listed above.
5. Report results by strategy and market regime.

Required metrics:

```text
trade count
win rate
average net PnL
profit factor
stop-out rate
max drawdown
average MFE/MAE
average holding days
results by PULLBACK/BREAKOUT
```

### Phase 2 - Implement entry/candidate redesign

Only after Phase 1 supports it:

1. Disable live `DEFENSE_LONG`.
2. Add slope/relative-strength indicators.
3. Redesign PULLBACK.
4. Adjust BREAKOUT.
5. Redesign candidate scanner ranking.
6. Fix incomplete-day volume comparison.
7. Add focused tests.

### Phase 3 - Exit policy and deployment

Select exit values from backtest evidence.

Do not edit server `.env` as the first step.

Update code defaults/tests, then deploy the selected policy after user approval.

## Tests Required

Add tests proving:

1. Downtrend bounce is rejected.
2. Strong uptrend with controlled pullback is accepted.
3. Strong individual stock can be accepted in WEAK market.
4. DEFENSE_LONG is not emitted.
5. Early-session partial volume does not incorrectly reject a strong candidate.
6. SMA224-dependent behavior is not used without sufficient lookback.
7. Exit policy behaves over a multi-day swing horizon.

Run:

```bash
python -m pytest tests/ -q
python -m compileall kospi_bot_v2 core main.py run_candidate_scan.py
```

## Current Repo State / Do Not Overwrite

Current recent commits:

```text
c4f1e1e fix KR bot entry delay: block repeated buy attempts and handle EGW00121
de910ef refactor: rename KOSPI Bot v2 labels to KR Bot throughout
05e8c8a tune KOSPI live bot for higher risk reward
```

Current untracked files:

```text
backtest_defense_long.py
docs/handoff/2026-06-04-kr-swing-strategy-redesign.md
docs/handoff/2026-06-04-codex-to-claude-kr-profit-first-swing-redesign.md
```

Do not delete or overwrite these files.

## Forbidden

- Do not expose/edit/commit `.env`, keys, tokens, account numbers.
- Do not commit or overwrite `data/*`, logs, live state, reports.
- Do not touch US bot, IRP bot, or retired coin bot.
- Do not deploy a wider stop before backtest evidence and user approval.
- Do not create a strategy that trades no stocks due to an unavailable SMA224.
- Do not revert the recent Claude commits.

## Required Report To User/Codex

Before implementation/deployment, report:

- Why the old candidate scanner selects falling/large-cap-biased stocks.
- Current vs SMA224-only vs relative-strength trend-pullback backtest.
- Exit-policy comparison.
- Recommended final candidate rules.
- Recommended final stop/take/trailing/holding-period rules.
- Exact files proposed for change.

