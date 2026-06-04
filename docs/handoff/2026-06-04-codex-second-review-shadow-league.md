# Codex Second Review: KR Shadow League Still Blocked

Date: 2026-06-04

## Decision

Do not deploy yet.

The first review's accounting, missing-price, versioned-state, and observed-stop
issues were substantially fixed. The focused suite now reports `51 passed`.
However, the actual systemd entry points fail immediately, and the daily path is
not holiday-safe or idempotent.

## Verified Improvements

- Round-trip cost now reconciles closed-trade cash and NAV.
- Daily NAV history and equity-curve MDD exist and are persisted.
- Missing current prices no longer fall back to stale closes for entry.
- Intraday stop exits use the observed lower price.
- State paths include strategy ID and version.
- Cash utilization uses open market value / NAV.
- A-v1 and D-v1 limitations are disclosed.

## P0: Required Before Deployment

1. Fix both systemd script entry points.
   - Actual smoke commands:
     - `python -m kospi_bot_v2.shadow.scripts.daily_finalize`
     - `python -m kospi_bot_v2.shadow.scripts.weekly_report`
   - Both currently fail with:
     `TypeError: MarketDataProvider() takes no arguments`
   - `MarketDataProvider` is abstract. Use a concrete quote provider or consume a
     persisted, validated end-of-day market snapshot produced by the live runner.

2. Do not swallow permanent script failures with exit code 0.
   - Both scripts currently catch every exception and call `sys.exit(0)`.
   - A systemd timer remains scheduled even when its oneshot service fails; returning
     nonzero is necessary so monitoring can detect the failure.
   - Expected data-unavailable cases may be handled explicitly, but programming and
     configuration errors must exit nonzero.

3. Add real KRX holiday detection to the daily entry point.
   - `daily_finalize.py` currently always calls `trading_day=True`.
   - Therefore a weekday KRX holiday increments `held_sessions`, records a NAV day,
     and may time-exit positions.
   - Use a trusted KRX calendar or validated market-data date. Never infer an open
     session only from Monday-Friday.

4. Make daily finalization idempotent.
   - Re-running the daily oneshot currently increments `held_sessions` again and appends
     a duplicate NAV snapshot for the same date.
   - Persist/check the last finalized trading date. A repeated run for the same date
     must not process exits or increment sessions twice.
   - `record_eod_nav()` should upsert by date rather than blindly append.

5. Submit and review the actual live-runner call-site integration.
   - `runner_integration.py` exists, but `kospi_bot_v2/runtime/live_runner.py` does not
     call `shadow_evaluate()` or `shadow_evaluate_exits()`.
   - The league currently cannot receive intraday candidates in production.

6. Add executable-path tests.
   - Tests must run both module entry points with a deterministic concrete provider.
   - Assert that a programming/configuration error returns nonzero.
   - Assert weekday holiday and same-date rerun do not increment held sessions.

## P1: Correct Before Using Promotion Decisions

1. Calculate promotion PF from monetary realized PnL, not unweighted trade percentages.
   - `league_stats()` still sums `pnl_pct`.
   - With different allocations, this can disagree with portfolio performance.
   - Persist or derive each trade's realized net PnL amount and use monetary gross
     profit / gross loss for the PF used by promotion rules.

2. Include intraday exits in the daily exit report.
   - Intraday exits are persisted, but `finalize_day()` reports only exits returned by
     `mark_day()`, so the user may not see stop/take exits that happened earlier.

## Re-Approval Evidence Required

- Focused tests pass.
- Both `python -m ...daily_finalize` and `python -m ...weekly_report` complete
  successfully against a deterministic provider.
- An intentional bad configuration makes the oneshot return nonzero.
- Holiday and same-date rerun tests pass.
- Actual `live_runner.py` integration diff is present and reviewed.
- Fresh-state dry run produces a report and internally reconciled state.
- `V2_NEW_ENTRIES_ENABLED=false` remains active.

