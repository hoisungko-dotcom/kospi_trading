from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BOOL_TRUE = {"1", "true", "yes", "on"}


class RuntimeBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeBootstrapSnapshot:
    bot_root: Path
    env_path: Path
    override_env_path: Path
    box_rt_watchlist_max: int
    universe_top_n: int
    universe_refresh_sec: int
    broker_sync_interval_sec: int
    no_buy_before: str


def load_runtime_env(bot_root: Path) -> tuple[Path, Path]:
    env_path = bot_root / ".env"
    override_env_path = bot_root / ".env.ai_overrides"
    load_dotenv(env_path)
    load_dotenv(override_env_path, override=True)
    return env_path, override_env_path


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in BOOL_TRUE


def _env_int(name: str, default: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name, default).strip() or default
    try:
        value = int(raw)
    except Exception as exc:
        raise RuntimeBootstrapError(f"{name} 값이 정수가 아닙니다: {raw}") from exc
    if minimum is not None and value < minimum:
        raise RuntimeBootstrapError(f"{name} 값이 너무 작습니다: {value} < {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeBootstrapError(f"{name} 값이 너무 큽니다: {value} > {maximum}")
    return value


def _validate_hhmm(name: str, default: str) -> str:
    value = os.getenv(name, default).strip() or default
    if len(value) != 4 or not value.isdigit():
        raise RuntimeBootstrapError(f"{name} 값이 HHMM 형식이 아닙니다: {value}")
    hh = int(value[:2])
    mm = int(value[2:])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise RuntimeBootstrapError(f"{name} 값이 유효한 시간이 아닙니다: {value}")
    return value


def ensure_runtime_dirs(bot_root: Path) -> None:
    for rel in ("data", "logs", "journal"):
        (bot_root / rel).mkdir(parents=True, exist_ok=True)


def bootstrap_runtime(bot_root: Path) -> RuntimeBootstrapSnapshot:
    env_path, override_env_path = load_runtime_env(bot_root)
    ensure_runtime_dirs(bot_root)

    watchlist_max = _env_int("BOX_RT_WATCHLIST_MAX", "12", minimum=1, maximum=50)
    universe_top_n = _env_int("BOX_BOT_UNIVERSE_TOP_N", "200", minimum=1, maximum=500)
    universe_refresh_sec = _env_int("BOX_BOT_UNIVERSE_REFRESH_SEC", "300", minimum=30, maximum=3600)
    _env_int("BOX_RT_UNIVERSE_REFRESH_SEC", "300", minimum=30, maximum=3600)
    broker_sync_interval_sec = _env_int("BROKER_SYNC_INTERVAL_SEC", os.getenv("KIS_SYNC_INTERVAL_SEC", "600"), minimum=60, maximum=3600)
    _env_int("PATTERN_HEARTBEAT_INTERVAL_MIN", "60", minimum=1, maximum=1440)
    max_new_buys_per_scan = _env_int("BOX_BOT_MAX_NEW_BUYS_PER_SCAN", "99", minimum=1, maximum=99)
    max_new_buys_per_10min = _env_int("BOX_BOT_MAX_NEW_BUYS_PER_10MIN", "99", minimum=1, maximum=99)
    focus_top_candidates = _env_int("BOX_BOT_FOCUS_TOP_CANDIDATES", "3", minimum=1, maximum=10)
    no_buy_before = _validate_hhmm("BOX_BOT_NO_BUY_BEFORE", "0920")
    opening_require_until = _validate_hhmm("BOX_BOT_OPENING_REQUIRE_PREFERRED_UNTIL", "1030")

    if int(no_buy_before) >= int(opening_require_until):
        raise RuntimeBootstrapError(
            "BOX_BOT_NO_BUY_BEFORE 는 BOX_BOT_OPENING_REQUIRE_PREFERRED_UNTIL 보다 빨라야 합니다"
        )
    if focus_top_candidates > watchlist_max:
        raise RuntimeBootstrapError(
            f"BOX_BOT_FOCUS_TOP_CANDIDATES({focus_top_candidates}) 가 BOX_RT_WATCHLIST_MAX({watchlist_max}) 보다 큽니다"
        )
    if max_new_buys_per_scan > max_new_buys_per_10min:
        raise RuntimeBootstrapError(
            f"BOX_BOT_MAX_NEW_BUYS_PER_SCAN({max_new_buys_per_scan}) 이 "
            f"BOX_BOT_MAX_NEW_BUYS_PER_10MIN({max_new_buys_per_10min}) 보다 큽니다"
        )
    return RuntimeBootstrapSnapshot(
        bot_root=bot_root,
        env_path=env_path,
        override_env_path=override_env_path,
        box_rt_watchlist_max=watchlist_max,
        universe_top_n=universe_top_n,
        universe_refresh_sec=universe_refresh_sec,
        broker_sync_interval_sec=broker_sync_interval_sec,
        no_buy_before=no_buy_before,
    )
