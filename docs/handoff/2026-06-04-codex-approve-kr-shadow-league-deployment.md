# Codex Approval: Deploy KR Shadow Strategy League

Date: 2026-06-04

## Decision

Approved for deployment with the existing live-buy kill switch kept active.

The shadow league is isolated from live orders, the post-close path is snapshot-only,
non-trading-day live exits/entries are blocked, state/report accounting is reconciled,
and the focused/full test suite passes.

## Verified

- Shadow league tests: `52 passed`
- Full repository tests: `71 passed`
- `git diff --check`: passed
- Compile check: passed
- `run_post_close_snapshot_only()` does not call broker sync/buy/sell/evaluate_exits.
- Post-close retry and finalizer-readability tests pass.
- Snapshot validation requires finality/session/trading-day fields.
- Weekday holiday/stale-session shadow evaluation and live order evaluation are skipped.

## Approved Commit Scope

Commit only the shadow-league implementation and its reviewed integration:

- `kospi_bot_v2/main.py`
- `kospi_bot_v2/runtime/live_runner.py`
- `kospi_bot_v2/shadow/**`
- `systemd/kr-shadow-daily.service`
- `systemd/kr-shadow-daily.timer`
- `systemd/kr-shadow-weekly.service`
- `systemd/kr-shadow-weekly.timer`
- `tests/test_shadow_league.py`
- relevant shadow-league handoff/design documentation only

Do not accidentally include unrelated untracked backtest scripts, runtime state, reports,
logs, `.env`, tokens, or keys.

## Deployment Requirements

1. Review staged diff and secret/state scan.
2. Commit and push to GitHub.
3. Pull code on AWS without overwriting `.env`, `data/`, or logs.
4. Install/reload the two shadow services and timers.
5. Keep `V2_NEW_ENTRIES_ENABLED=false`.
6. Restart `kr-bot.service`.
7. Verify:
   - `systemctl is-active kr-bot.service`
   - `systemctl is-enabled --quiet kr-shadow-daily.timer`
   - `systemctl is-enabled --quiet kr-shadow-weekly.timer`
   - process environment still contains `V2_NEW_ENTRIES_ENABLED=false`
8. Run the oneshot services manually against a controlled valid snapshot and verify
   successful exit plus generated report.
9. Inspect recent logs for shadow exceptions and confirm no live buy attempt escaped the
   kill switch.

## Residual Operational Risk

If `kr-bot.service` starts only after the 15:30 KST active-to-inactive transition, the
post-close snapshot will not be created automatically that day. Treat a missing weekday
snapshot at 16:00 as an alert and manually run the snapshot-only path before finalizing.
This does not create a live-order risk.

