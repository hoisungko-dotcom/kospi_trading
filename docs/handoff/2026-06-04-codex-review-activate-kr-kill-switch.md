# Codex Review - Activate KR Kill Switch

Date: 2026-06-04

## Review Result

Local implementation is valid and focused:

- `KISLiveBroker.buy()` blocks new buys when `V2_NEW_ENTRIES_ENABLED=false`.
- Sells, sync, diagnostics, and signal generation remain available.
- Local verification: `19 passed`.

However, the kill switch is not active on AWS yet.

Observed server state:

```text
kr-bot.service: active
server commit: c4f1e1e
V2_NEW_ENTRIES_ENABLED: not set
```

Therefore the live server can still place new buy orders.

## Important Correction

The switch is read at each `buy()` call from the process environment, but `.env` or systemd environment changes are not live-reloaded into an already-running Python process.

Changing `.env` or a systemd drop-in requires restarting `kr-bot.service`.

## Required Immediate Action

1. Commit the safe code/test files:

```text
kospi_bot_v2/portfolio/live_broker.py
kospi_bot_v2/strategy/signal_engine.py
tests/test_live_trade_safety.py
tests/test_backtest_audit.py
```

Do not commit backtest outputs, runtime data, logs, or secrets.

2. Push and deploy code.

3. Activate the switch on AWS without exposing secrets.

Preferred systemd drop-in:

```bash
sudo mkdir -p /etc/systemd/system/kr-bot.service.d
printf '[Service]\nEnvironment=V2_NEW_ENTRIES_ENABLED=false\n' \
  | sudo tee /etc/systemd/system/kr-bot.service.d/disable-new-entries.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart kr-bot.service
```

4. Verify:

```bash
sudo systemctl is-active kr-bot.service
sudo systemctl show kr-bot.service -p Environment --no-pager
journalctl -u kr-bot.service --since "2 minutes ago" --no-pager -n 100
```

5. Run a safe verification that does not place an order:

- Confirm the service process environment contains `V2_NEW_ENTRIES_ENABLED=false`.
- Confirm startup/balance sync succeeds.
- Do not manually call a real `buy()` method merely to test the switch.

## Future Warning

The current uncommitted `signal_engine.py` change removes `DEFENSE_LONG`, but also allows WEAK-regime signals to fall through into normal PULLBACK/BREAKOUT logic.

This is harmless while the kill switch remains active.

Before ever re-enabling live entries:

- review WEAK-regime eligibility
- require an audited positive-expectancy replacement strategy
- obtain user approval

## Report Back

Report:

- commit hash
- push/deploy result
- drop-in activation result
- `kr-bot.service` status
- confirmation that new entries are disabled
- confirmation that exits/monitoring remain active

