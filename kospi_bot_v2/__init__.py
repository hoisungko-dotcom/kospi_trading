"""Korean stock shadow trading bot v2.

This package is intentionally isolated from the legacy live bot. The first
runtime is paper/shadow only: it may read market data, but it never submits
orders to a broker.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
