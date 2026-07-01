# Strategy

Purpose:
- pure trading logic
- signal generation
- risk rules
- exit rules

Primary strategy identity:
- `Box Bot`

Current authority:
- live strategy authority = `main.py`
- `strategy/` contains canonical strategy logic extracted from that live engine
- `kospi_bot_v2` strategy logic is support/shadow, not a separate primary identity

Target contents over time:
- box strategy
- scalp variants
- regime gates
- signal engines

Current source migration candidates:
- `core/signal_analyzer_kospi.py`
- `core/dynamic_exit_analyzer_kospi.py`
- `core/risk_management.py`
- `kospi_bot_v2/strategy/`
- `kospi_bot_v2/risk/`
