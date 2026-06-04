# Codex Third Review: KR Shadow League Deployment Still Blocked

Date: 2026-06-04

## Decision

Do not deploy yet.

The focused suite passes (`58 passed`) and the prior mechanical fixes are present.
However, several claims in the completion report do not match the production path.
These issues can cause false trading days, missing daily trades, and invalid strategy
comparisons.

## P0: Required Before Deployment

1. Implement actual KRX trading-day detection.
   - The snapshot supports `trading_day`, but `live_runner.py` calls `save_snapshot()`
     without passing it, so every written snapshot uses the default `True`.
   - On a weekday KRX holiday, the live loop can read stale last-session daily bars,
     write a snapshot dated today, and cause held-session increments/time exits.
   - Determine trading-day status from a trusted KRX calendar or validated market-data
     session date. Pass the explicit result to `save_snapshot()`.

2. Validate snapshot freshness and finality.
   - The live runner overwrites the so-called EOD snapshot on every intraday loop.
   - The snapshot has no `generated_at`, source-session date, or final/complete marker.
   - A bot outage can leave a morning snapshot that the 16:00 job treats as EOD.
   - Persist and validate `generated_at`, market session date, and a finalization cutoff
     or explicit final marker before processing EOD exits/NAV.

3. Persist or reconstruct same-day entry/exit logs.
   - `_entries_today_log`, `_exits_today_log`, and `_entries_today` are in-memory only.
   - The 16:00 daily service creates a new `ShadowLeague`, so intraday exits will not
     appear in its report despite the completion claim.
   - A live-runner restart can also reset the daily-entry limit and allow another entry.
   - Persist daily event records/counters, or reconstruct them from persisted trades and
     positions using dates. Add restart-to-daily-report and restart-entry-limit tests.

4. Exclude market proxy rows from tradable candidates.
   - The frame includes `KSPI` and `KDQ` proxy rows and the shadow ranking currently
     evaluates every latest row.
   - Strategies may create virtual positions in non-tradable proxy symbols.
   - Filter proxies explicitly and add a test proving no strategy can enter them.

5. Clarify and fix strategy-universe independence.
   - In live mode, `main.py` replaces the provider universe with legacy
     `top_10_daily.json` candidates.
   - Therefore B/C/D do not receive an independent broad raw universe; they receive the
     upstream legacy screener's selected pool.
   - For a valid strategy league, supply a shared broad investable universe independent
     of A's screener, or clearly rename the experiment as an exit/entry-rule comparison
     within the legacy screened pool.

6. Do not treat a missing 16:00 snapshot as a holiday by default.
   - Missing snapshot also means live-runner outage or snapshot-write failure.
   - Return success only when a trusted calendar confirms a holiday. Otherwise exit
     nonzero and alert.

## P1: Data Integrity

1. Do not silently start fresh after state corruption.
   - `ShadowPortfolio._load()` catches every exception and continues with a fresh
     account, which can erase the validity of the league history.
   - Fail loudly or quarantine the corrupt state and alert.

2. Make `meta.json` writes atomic, like portfolio and snapshot writes.

## Verified Evidence

- Focused tests: `58 passed in 5.25s`
- Compile check passed.
- No-snapshot daily and weekly scripts return `0` as implemented.
- Actual live-runner diff is present.
- No Git commit, push, AWS deployment, or service restart was performed by Codex.

## Re-Approval Tests

- Weekday KRX holiday produces `trading_day=false` without relying on missing snapshot.
- Stale/non-final snapshot is rejected at 16:00.
- Intraday exit remains visible after process restart and in the daily report.
- Daily entry limit survives live-runner restart.
- `KSPI`/`KDQ` can never become positions.
- League receives the intended independent universe.
- Missing snapshot on an open market day returns nonzero and alerts.

