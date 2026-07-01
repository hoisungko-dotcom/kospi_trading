from __future__ import annotations

from brokers.kis.client import KISClientKospi
from brokers.kiwoom.client import KiwoomClientKospi
from core.broker_interfaces import BrokerClient
from core.broker_profile import BrokerProfile
from services.null_sector_monitor import NullSectorMonitor
from services.sector_monitor import SectorMonitor


def create_broker_client(profile: BrokerProfile) -> BrokerClient:
    if profile.broker == "kis":
        return KISClientKospi()
    if profile.broker == "kiwoom":
        return KiwoomClientKospi()
    if profile.execution == "kis":
        return KISClientKospi()
    return KiwoomClientKospi()


def create_sector_monitor(profile: BrokerProfile, broker_client):
    if profile.sector_mode == "kis":
        return SectorMonitor(broker_client._client)
    return NullSectorMonitor()
