from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class BrokerProfile:
    name: str
    broker: str
    market_data: str
    execution: str
    account: str
    realtime: str
    universe: str
    sector_mode: str


_PROFILES: dict[str, BrokerProfile] = {
    "kiwoom_full": BrokerProfile(
        name="kiwoom_full",
        broker="kiwoom",
        market_data="kiwoom",
        execution="kiwoom",
        account="kiwoom",
        realtime="kiwoom",
        universe="kiwoom",
        sector_mode="none",
    ),
    "kis_full": BrokerProfile(
        name="kis_full",
        broker="kis",
        market_data="kis",
        execution="kis",
        account="kis",
        realtime="kis",
        universe="kis",
        sector_mode="kis",
    ),
    "hybrid_safe": BrokerProfile(
        name="hybrid_safe",
        broker="hybrid",
        market_data="kiwoom",
        execution="kis",
        account="kis",
        realtime="kiwoom",
        universe="kiwoom",
        sector_mode="kis",
    ),
}


def load_broker_profile() -> BrokerProfile:
    profile_name = (os.getenv("BROKER_PROFILE", "kiwoom_full").strip().lower() or "kiwoom_full")
    return _PROFILES.get(profile_name, _PROFILES["kiwoom_full"])

