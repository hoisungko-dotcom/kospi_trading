# Codex Fifth Review: KR Shadow League Deployment Blocked by Post-Close Live-Order Risk

Date: 2026-06-04

## Decision

Do not deploy yet.

The shadow test file passes `48` tests and the combined focused suite passes `67`.
The fourth-review date/holiday changes are present. However, the new post-close
implementation executes the entire live trading cycle after market close. This can
submit real sell orders and, after the buy kill switch is eventually removed, real buy
orders.

## P0: Required Before Deployment

1. Replace the post-close full `run_once()` with a snapshot-only path.
   - `main.py` calls `run_and_print()` on the active-to-inactive transition.
   - This invokes `LiveRunner.run_once()`, which calls:
     - `broker.sync()`
     - `broker.evaluate_exits()` and potentially `broker.sell()`
     - signal generation and minute confirmation
     - `broker.buy()`
   - `V2_NEW_ENTRIES_ENABLED=false` blocks buys only. It does not block sells.
   - `RiskGuard` contains no market-hours restriction.
   - Create an explicit `write_post_close_snapshot()` / `run_post_close_snapshot_only()`
     method that fetches/validates quote data and writes the final shadow snapshot
     without touching the live broker, live exits, live buys, or live reports.

2. Guard all live orders by market-session status independently of the shadow league.
   - The current `trading_day` check occurs after `broker.evaluate_exits()`.
   - On a weekday holiday or post-close stale quote run, real exits can be evaluated
     before the non-trading-day guard.
   - Add a broker/order-boundary market-hours/session guard. Keep emergency/manual
     liquidation as a separate explicit path if needed.

3. Retry post-close snapshot creation after transient failure.
   - `_post_close_done=True` is set before the attempt.
   - If the one attempt fails, the process never retries and the 16:00 finalizer fails.
   - Mark completion only after a validated final snapshot exists. Retry with bounded
     backoff until the reporting deadline.

4. Add an actual post-close transition integration test.
   - Current new tests validate snapshot helpers, but none exercises the `main.py`
     active-to-inactive branch.
   - Test that the transition:
     - writes exactly one final snapshot,
     - never calls live `buy`, `sell`, or `evaluate_exits`,
     - retries after a transient snapshot failure,
     - permits the 16:00 finalizer to succeed.

5. Require all snapshot validation fields.
   - `load_snapshot()` currently requires only legacy fields and defaults missing
     `session_date`, `trading_day`, and `is_final` to values that pass validation.
   - A legacy or malformed snapshot can therefore bypass finality/freshness checks.
   - Require and validate `session_date`, `generated_at`, `is_final`, and `trading_day`.

## P1: Clean-Up

1. The comment in `live_runner.py` says `is_final` is recomputed from current wall time,
   but it uses `_now_kst` captured at the start of `run_once()`. Either recompute it
   immediately before saving or correct the comment.

2. Use the shared KST helper consistently when restoring persisted daily portfolio logs.

## Verified Evidence

- `python -m pytest tests/test_shadow_league.py -q`: `48 passed`
- Combined focused suite: `67 passed`
- Compile check and `git diff --check`: passed
- A minimal legacy snapshot without finality/session fields was accepted by
  `load_and_validate_snapshot()`, confirming the validation bypass.
- No Git commit, push, AWS deployment, timer installation, or service restart performed.
- Keep `V2_NEW_ENTRIES_ENABLED=false`.

## Re-Approval Criteria

- Post-close operation is snapshot-only and cannot reach any live-order method.
- Non-trading/post-close runs cannot evaluate or submit live exits or entries.
- Post-close snapshot creation retries safely and produces one accepted final snapshot.
- The real active-to-inactive transition test passes.
- Legacy/malformed snapshots are rejected.

