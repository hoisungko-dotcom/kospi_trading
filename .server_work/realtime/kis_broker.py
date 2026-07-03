from __future__ import annotations

from realtime.kis_mock_broker import KisMockDomesticBroker

__all__ = ["KisMockDomesticBroker", "KisDomesticBroker"]


class KisDomesticBroker(KisMockDomesticBroker):
    """Neutral runtime alias for the KIS domestic broker."""

