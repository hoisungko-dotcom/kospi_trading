# Codex Review: KR Shadow League Deployment Blocked

Date: 2026-06-04

## Decision

Do not deploy the current shadow league patch.

The live-order isolation and kill switch are good, and the reported 45 tests pass.
However, the current implementation can rank strategies using incorrect NAV, daily
PnL, MDD, transaction costs, holding periods, and execution prices. The systemd
services also reference modules that do not exist in the current patch.

Keep `V2_NEW_ENTRIES_ENABLED=false` and `kr-bot.service` active.

## P0: Must Fix Before Deployment

1. Apply transaction costs to portfolio cash and NAV.
   - `ShadowTrade.pnl_pct` deducts `ROUND_TRIP_COST`, but `_close_position()` returns
     the full sale proceeds to cash.
   - This makes NAV and cash-benchmark comparisons ignore costs while PF includes them.
   - Use an explicit entry/exit fee model and make cash, NAV, and trade metrics reconcile.
   - Add a deterministic test asserting final cash/NAV after one round trip.

2. Track portfolio-level daily NAV history and calculate reports from it.
   - Daily PnL is currently the sum of trade return percentages.
   - MDD is currently calculated from the cumulative sum of trade percentages.
   - Both are wrong with variable allocations and concurrent positions.
   - Persist daily snapshots per strategy. Calculate daily PnL, cumulative return, and
     MDD from the NAV/equity curve.
   - Add tests with unequal position sizes and overlapping positions.

3. Use trading sessions, not calendar days, for max hold.
   - `mark_day()` uses `(trade_date - entry_date).days`, so weekends and holidays count.
   - Persist `held_sessions` on each position and increment it only once per confirmed
     KRX trading day.
   - Add Friday-entry and holiday tests.

4. Do not enter with stale fallback prices.
   - `league.evaluate()` counts a missing live price, then falls back to `row.close` and
     may still enter.
   - If the execution price is missing, skip the entry and record the data-quality event.
   - Add a test proving missing current price cannot create a position.

5. Fix intraday execution-price optimism.
   - `evaluate_intraday_exits()` observes a price below the stop but exits at the higher
     stop price.
   - Use the observed price for stop exits, or a documented conservative execution model.
   - Add a gap/fast-drop test where observed price is below the stop.

6. Add the executable timer modules before installing timers.
   - Services reference:
     - `kospi_bot_v2.shadow.scripts.daily_finalize`
     - `kospi_bot_v2.shadow.scripts.weekly_report`
   - No `kospi_bot_v2/shadow/scripts/` files exist in the reviewed patch.
   - Test both oneshot services locally/AWS before enabling timers.

## P1: Must Resolve In The Same Patch

1. A-v1 is not an exact live control.
   - The document calls it exact, but live uses a conditional 45-minute time stop while
     shadow uses a one-day EOD stop.
   - Either reproduce the live behavior or rename it clearly as an approximation.

2. Version state paths must be collision-safe.
   - Current state path is `state_{strategy_id}.json`.
   - The stated immutable-version policy requires a key such as
     `state_{strategy_id}_{version}.json`, plus a migration decision.

3. Fix cash utilization.
   - Current formula can become negative after realized profit.
   - Use open-position market value divided by current NAV.

4. Review the live-runner integration as code, not as a promised future 10-line edit.
   - Submit the integration patch before deployment.
   - It must not block or crash the live monitoring loop if shadow evaluation fails.

5. State clearly that D-v1 is inactive until the live data pipeline provides valid
   SMA224 values with sufficient history.

## Verification Required For Re-Approval

- Full focused tests pass, including the new accounting, NAV/MDD, trading-session,
  missing-price, and execution tests.
- Fresh-state one-day dry run produces internally reconciling cash + positions = NAV.
- Daily and weekly oneshot services run successfully.
- Timers are enabled only after the services succeed.
- Git diff contains no `.env`, secrets, runtime state, generated reports, or logs.
- `V2_NEW_ENTRIES_ENABLED=false` remains active.
- `kr-bot.service` remains active and recent logs show no shadow-loop exception.

## Reviewed State

- Tests run by Codex:
  `python -m pytest tests/test_live_trade_safety.py tests/test_backtest_audit.py tests/test_shadow_league.py -q`
- Result: `45 passed`
- Current shadow files and systemd files are untracked.
- No shadow live-runner integration was present in the reviewed diff.

