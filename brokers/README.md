# Brokers

Purpose:
- broker-specific implementations only

Target contents over time:
- `brokers/kis/`
- `brokers/kiwoom/`
- future broker adapters

Rule:
- broker brand names are allowed here
- common strategy/runtime code should not live here

Current source migration candidates:
- `core/kis_client_kospi.py`
- `core/kiwoom_client_kospi.py`
- `core/api_client.py`

Canonical broker-runtime files now include:
- `brokers/kis/quote_provider.py`
- `brokers/kiwoom/auth.py`
- `brokers/kiwoom/data_provider.py`

Compatibility wrappers remain at:
- `kospi_bot_v2/market/kis_quote_provider.py`
- `kospi_bot_v2/market/kiwoom_client.py`
- `kospi_bot_v2/market/kiwoom_data_provider.py`
