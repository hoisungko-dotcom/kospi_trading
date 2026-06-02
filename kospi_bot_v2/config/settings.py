from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ShadowSettings:
    """Runtime settings for the shadow bot.

    The bot defaults to paper trading and refuses to expose a live order path.
    Market data can be supplied from CSV first, then later from a KIS quote-only
    adapter.
    """

    initial_cash: float = 10_000_000
    max_positions_bull: int = 8
    max_positions_neutral: int = 8
    max_positions_weak: int = 6
    max_positions_crash: int = 3
    base_position_pct: float = 0.14
    max_position_pct: float = 0.20
    daily_loss_limit_pct: float = -0.02
    stop_loss_pct: float = -0.010
    take_profit_pct: float = 0.030
    trailing_start_pct: float = 0.018
    trailing_gap_pct: float = 0.008
    reentry_cooldown_minutes: int = 180
    time_stop_minutes: int = 45
    min_score_bull: float = 64
    min_score_neutral: float = 68
    min_score_weak: float = 72
    universe_symbols: tuple[str, ...] = (
        "005930",
        "000660",
        "005380",
        "000270",
        "068270",
        "035420",
        "005490",
        "042660",
        "196170",
        "247540",
        "028300",
        "086520",
        "252670",
        "251340",
        "114800",
    )
    loop_interval_sec: int = 45
    active_start_hhmm: int = 830
    active_end_hhmm: int = 1530
    active_timezone: str = "Asia/Seoul"
    include_account_snapshot: bool = True
    compare_log_path: Path = PROJECT_ROOT / "logs" / "kospi_trading.log"
    report_dir: Path = V2_ROOT / "reports"
    ledger_path: Path = V2_ROOT / "data" / "shadow_ledger.jsonl"


def load_settings() -> ShadowSettings:
    """Load settings from environment with safe defaults."""

    load_dotenv(PROJECT_ROOT / ".env", override=False)

    def number(name: str, default: float) -> float:
        raw = os.getenv(f"V2_{name}")
        if raw is None or raw == "":
            return default
        return float(raw)

    def integer(name: str, default: int) -> int:
        raw = os.getenv(f"V2_{name}")
        if raw is None or raw == "":
            return default
        return int(raw)

    def symbols(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = os.getenv(f"V2_{name}")
        if not raw:
            return default
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    def boolean(name: str, default: bool) -> bool:
        raw = os.getenv(f"V2_{name}")
        if raw is None or raw == "":
            return default
        return raw.lower() in {"1", "true", "yes", "on"}

    default = ShadowSettings()
    return ShadowSettings(
        initial_cash=number("INITIAL_CASH", default.initial_cash),
        max_positions_bull=integer("MAX_POSITIONS_BULL", default.max_positions_bull),
        max_positions_neutral=integer("MAX_POSITIONS_NEUTRAL", default.max_positions_neutral),
        max_positions_weak=integer("MAX_POSITIONS_WEAK", default.max_positions_weak),
        max_positions_crash=integer("MAX_POSITIONS_CRASH", default.max_positions_crash),
        base_position_pct=number("BASE_POSITION_PCT", default.base_position_pct),
        max_position_pct=number("MAX_POSITION_PCT", default.max_position_pct),
        daily_loss_limit_pct=number("DAILY_LOSS_LIMIT_PCT", default.daily_loss_limit_pct),
        stop_loss_pct=number("STOP_LOSS_PCT", default.stop_loss_pct),
        take_profit_pct=number("TAKE_PROFIT_PCT", default.take_profit_pct),
        trailing_start_pct=number("TRAILING_START_PCT", default.trailing_start_pct),
        trailing_gap_pct=number("TRAILING_GAP_PCT", default.trailing_gap_pct),
        reentry_cooldown_minutes=integer("REENTRY_COOLDOWN_MINUTES", default.reentry_cooldown_minutes),
        time_stop_minutes=integer("TIME_STOP_MINUTES", default.time_stop_minutes),
        min_score_bull=number("MIN_SCORE_BULL", default.min_score_bull),
        min_score_neutral=number("MIN_SCORE_NEUTRAL", default.min_score_neutral),
        min_score_weak=number("MIN_SCORE_WEAK", default.min_score_weak),
        universe_symbols=symbols("UNIVERSE_SYMBOLS", default.universe_symbols),
        loop_interval_sec=integer("LOOP_INTERVAL_SEC", default.loop_interval_sec),
        active_start_hhmm=integer("ACTIVE_START_HHMM", default.active_start_hhmm),
        active_end_hhmm=integer("ACTIVE_END_HHMM", default.active_end_hhmm),
        active_timezone=os.getenv("V2_ACTIVE_TIMEZONE", default.active_timezone),
        include_account_snapshot=boolean("INCLUDE_ACCOUNT_SNAPSHOT", default.include_account_snapshot),
        compare_log_path=Path(os.getenv("V2_COMPARE_LOG_PATH", str(default.compare_log_path))).expanduser(),
        report_dir=Path(os.getenv("V2_REPORT_DIR", str(default.report_dir))).expanduser(),
        ledger_path=Path(os.getenv("V2_LEDGER_PATH", str(default.ledger_path))).expanduser(),
    )
