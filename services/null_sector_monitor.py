import logging

logger = logging.getLogger(__name__)


class NullSectorMonitor:
    """Fallback monitor used when the active broker does not support KIS sector APIs."""

    def update(self, force: bool = False):
        return

    def get_sector_bonus(self, symbol: str) -> float:
        return 0.0

    def get_sector_name(self, symbol: str) -> str | None:
        return None
