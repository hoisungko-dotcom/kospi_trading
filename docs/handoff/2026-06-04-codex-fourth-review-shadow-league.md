# Codex Fourth Review: KR Shadow League Deployment Blocked by Runtime Boundaries

Date: 2026-06-04

## Decision

Do not deploy yet.

The focused suite passes (`64 passed`), but the current production scheduling path
cannot produce a valid final snapshot and can evaluate shadow entries on KRX holidays.

## P0: Runtime-Blocking Findings

1. A final snapshot is never produced by the normal live loop.
   - `is_active_time()` uses:
     `active_start_hhmm <= current_hhmm < active_end_hhmm`
   - `active_end_hhmm` is `1530`.
   - `is_final=True` requires current time `>= 15:30`.
   - Therefore `run_once()` is never called at a time when it can write
     `is_final=True`. The last snapshot remains non-final and the 16:00 daily
     finalizer exits 1 every trading day.
   - Fix by adding a dedicated post-close snapshot/finalization step or extending the
     loop window with an explicit no-order post-close run. Add an integration test for
     the 15:29 → 15:30 → 16:00 sequence.

2. Shadow entries can be evaluated on KRX holidays using stale data.
   - `shadow_evaluate(...)` runs before `trading_day` is derived.
   - On a weekday holiday, the provider can return the prior session's last daily bar,
     but shadow strategies receive it with today's `trade_date` and may open positions.
   - Derive/validate session status before calling `shadow_evaluate`. When the market
     session is stale/non-trading, skip all shadow entry and exit evaluation.

3. KST date must be used instead of host-local `date.today()`.
   - AWS is operated with UTC-based timers. During 08:30-08:59 KST it is still the
     previous UTC date.
   - `trade_date = date.today().isoformat()` can therefore write/reset state under the
     wrong date during the first 30 minutes of the configured active window.
   - Use one timezone-aware KST clock for live-runner trade dates, snapshot paths,
     portfolio daily restoration, and daily script validation.
   - Add a deterministic test for 08:30 KST / 23:30 UTC.

4. Holiday snapshots conflict with snapshot validation.
   - The runner writes `trading_day=False` when `session_date != today`.
   - `load_and_validate_snapshot()` rejects every `session_date != trade_date` before
     the daily finalizer can process the snapshot as a holiday.
   - Decide one explicit policy:
     - trusted holiday/non-trading snapshot: accept only when `trading_day=False`,
       generate a holiday report, and never process exits; or
     - no holiday snapshot: skip writing and use a trusted KRX calendar in the finalizer.
   - Do not label stale data as a valid holiday without a trusted determination.

## P1: Correctness Follow-Up

1. Treat naive market timestamps according to their actual source timezone.
   - `KISQuoteOnlyProvider._timestamp()` creates naive timestamps representing KST,
     while `live_runner.py` currently treats naive timestamps as UTC.
   - Make the timestamp contract explicit and timezone-aware at the provider boundary.

2. Add a real runtime integration test.
   - Mock the provider clock/session and run the live-loop decision plus daily finalizer.
   - Unit tests of `save_snapshot()` alone do not expose the scheduling contradiction.

## Verified Evidence

- Focused tests: `64 passed in 5.31s`
- Compile and `git diff --check`: passed
- No Git commit, push, AWS deployment, timer installation, or service restart performed.
- Existing real-buy kill switch must remain enabled.

## Re-Approval Criteria

- A normal trading day produces exactly one accepted final snapshot after close.
- The 16:00 finalizer succeeds using that snapshot.
- A weekday KRX holiday produces no shadow entries/exits/session increments.
- 08:30 KST uses the correct KST trade date on an AWS UTC host.
- Holiday policy and stale-data policy are distinct and tested.

