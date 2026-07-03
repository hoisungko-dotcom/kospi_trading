"""
한국 박스봇 실운용 데일리 러너

매 1분마다 KOSPI 상위 종목만 스캔해 BoxChecker v2 박스 돌파 신호로 진입한다.
보유 포지션은 박스 청산 규칙과 손절 규칙으로 관리한다.
신규 매수는 15:00까지만 허용하고, 남은 포지션은 장 마감 전에 강제 청산한다.
15:30 EOD 텔레그램 리포트.

실행: python -m realtime.daily_runner
     python -m realtime.daily_runner --top 200 --delay 0.15

운영 경로는 BoxChecker v2 단일 전략만 허용한다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env")
load_dotenv(Path(__file__).parents[1] / ".env.ai_overrides", override=True)

from collector.kiwoom_client import get_stock_list
from collector.kis_real_client import kis_enabled, get_basic_price as kis_get_basic_price, get_min_chart as kis_get_min_chart, parse_candle as kis_parse_candle, get_volume_rank as kis_get_volume_rank, get_volume_power as kis_get_volume_power, get_foreign_institution_total as kis_get_foreign_institution_total
from collector.kiwoom_client import get_basic_price as kiwoom_get_basic_price, get_min_chart as kiwoom_get_min_chart, parse_candle as kiwoom_parse_candle
from collector.surge_detector import Candle
from ai_reviewer import ProfitReviewAgent
from realtime.strategy_state_engine import StrategyStateEngine
from realtime.box_checker import BoxChecker
from realtime.box_ladder_exit import BoxLadderExit
from realtime.kiwoom_realtime import KiwoomRealtimeClient, RealtimeTick
from realtime.realtime_strategy import BoxRealtimeState, RealtimeStateMachine
from realtime.kis_broker import KisDomesticBroker
from realtime.kis_realtime import KisRealtimeClient

KST = ZoneInfo("Asia/Seoul")
_LOG_ROOT = Path(__file__).parents[1] / "logs"
_LOG_ROOT.mkdir(parents=True, exist_ok=True)
_RUNTIME_LOG = _LOG_ROOT / "live_runtime.log"
_runtime_handler = TimedRotatingFileHandler(_RUNTIME_LOG, when="midnight", interval=1, backupCount=14, encoding="utf-8")
_runtime_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(), _runtime_handler],
    force=True,
)
logger = logging.getLogger(__name__)

LEGACY_BOX_ENV_KEYS = (
    "BOX_ENTRY_END_HOUR",
    "BOX_PREFERRED_MIN_HEIGHT_PCT_V1",
    "BOX_PREFERRED_MAX_HEIGHT_PCT_V1",
    "BOX_MIN_LENGTH_V1",
    "BOX_MAX_LENGTH_V1",
)


def _write_session_marker(message: str) -> None:
    try:
        with _RUNTIME_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(KST).strftime('%H:%M:%S')} INFO {message}\n")
    except Exception:
        logger.exception("runtime session marker write failed")


def _guard_legacy_box_env() -> None:
    found = [key for key in LEGACY_BOX_ENV_KEYS if os.getenv(key)]
    if not found:
        return
    logger.error("legacy v1 설정 감지: %s — 운영 경로는 BoxChecker v2 전용", found)
    raise SystemExit(1)


_guard_legacy_box_env()

USE_KIS_REAL_DATA = kis_enabled()
get_basic_price = kis_get_basic_price if USE_KIS_REAL_DATA else kiwoom_get_basic_price
get_min_chart = kis_get_min_chart if USE_KIS_REAL_DATA else kiwoom_get_min_chart
parse_candle = kis_parse_candle if USE_KIS_REAL_DATA else kiwoom_parse_candle

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MARKET_OPEN  = (9, 0)
NO_NEW_BUY   = (15, 0)    # 15:00 이후 신규 진입 중단
FORCE_FLAT   = (15, 18)   # 15:18 장마감 전 강제 청산
SEND_REPORT  = (15, 30)   # EOD 리포트 발송
AI_REVIEW_ENABLED = os.getenv("BOX_BOT_AI_REVIEW_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
AI_REVIEW_REWARD_TO_RISK = float(os.getenv("BOX_BOT_REWARD_TO_RISK_TARGET", "2.0") or 2.0)
REQUIRE_PREFERRED_BOX = os.getenv("BOX_BOT_REQUIRE_PREFERRED_BOX", "false").lower() in {"1", "true", "yes", "on"}
MAX_NEW_BUYS_PER_SCAN = int(os.getenv("BOX_BOT_MAX_NEW_BUYS_PER_SCAN", "99") or 99)
MAX_NEW_BUYS_PER_10MIN = int(os.getenv("BOX_BOT_MAX_NEW_BUYS_PER_10MIN", "99") or 99)
FOCUS_TOP_CANDIDATES = int(os.getenv("BOX_BOT_FOCUS_TOP_CANDIDATES", "1") or 1)
FOCUS_SINGLE_MODE = os.getenv("BOX_BOT_FOCUS_SINGLE_MODE", "true").lower() in {"1", "true", "yes", "on"}
FOCUS_TOP2_RATIO = float(os.getenv("BOX_BOT_FOCUS_TOP2_RATIO", "0.95") or 0.95)
NO_BUY_BEFORE = os.getenv("BOX_BOT_NO_BUY_BEFORE", "0920").strip() or "0920"
OPENING_REQUIRE_PREFERRED_UNTIL = os.getenv("BOX_BOT_OPENING_REQUIRE_PREFERRED_UNTIL", "1030").strip() or "1030"
OPENING_MAX_BUYS_PER_SCAN = int(os.getenv("BOX_BOT_OPENING_MAX_BUYS_PER_SCAN", "1") or 1)
OPENING_MAX_BUYS_PER_10MIN = int(os.getenv("BOX_BOT_OPENING_MAX_BUYS_PER_10MIN", "2") or 2)
HEARTBEAT_ENABLED = os.getenv("PATTERN_HEARTBEAT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
HEARTBEAT_INTERVAL_MIN = int(os.getenv("PATTERN_HEARTBEAT_INTERVAL_MIN", "60") or 60)
ALERT_COOLDOWN_SEC = int(os.getenv("PATTERN_ALERT_COOLDOWN_SEC", "900") or 900)
KIS_SYNC_INTERVAL_SEC = int(os.getenv("KIS_SYNC_INTERVAL_SEC", "600") or 600)
KIS_AUTO_LIQUIDATE_EXCLUDED = os.getenv("KIS_AUTO_LIQUIDATE_EXCLUDED", "true").lower() in {"1", "true", "yes", "on"}
KIS_EXCLUDED_RETRY_INTERVAL_SEC = int(os.getenv("KIS_EXCLUDED_RETRY_INTERVAL_SEC", "900") or 900)
BOX_RT_ENABLED = os.getenv("BOX_RT_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
BOX_RT_WATCHLIST_MAX = int(os.getenv("BOX_RT_WATCHLIST_MAX", "12") or 12)
BOX_RT_UNIVERSE_REFRESH_SEC = int(os.getenv("BOX_RT_UNIVERSE_REFRESH_SEC", "300") or 300)
BOX_RT_FREEZE_WATCHLIST_WHILE_HOLDING = os.getenv("BOX_RT_FREEZE_WATCHLIST_WHILE_HOLDING", "true").lower() in {"1", "true", "yes", "on"}
BOX_RT_DEGRADED_REST_SEC = int(os.getenv("BOX_RT_DEGRADED_REST_SEC", "20") or 20)
BOX_RT_HYBRID_PRICE_REFRESH_SEC = int(os.getenv("BOX_RT_HYBRID_PRICE_REFRESH_SEC", "8") or 8)
BOX_RT_LOSS_REENTRY_COOLDOWN_SEC = int(os.getenv("BOX_RT_LOSS_REENTRY_COOLDOWN_SEC", "1800") or 1800)
BOX_RT_FOLLOW_FAIL_REENTRY_COOLDOWN_SEC = int(os.getenv("BOX_RT_FOLLOW_FAIL_REENTRY_COOLDOWN_SEC", "3600") or 3600)
BOX_RT_MAX_ATTEMPTS_PER_SYMBOL_PER_DAY = int(os.getenv("BOX_RT_MAX_ATTEMPTS_PER_SYMBOL_PER_DAY", "2") or 2)
BOX_RT_MAX_LOSSES_PER_SYMBOL_PER_DAY = int(os.getenv("BOX_RT_MAX_LOSSES_PER_SYMBOL_PER_DAY", "2") or 2)
BOX_BOT_DAILY_MAX_LOSS_KRW = int(os.getenv("BOX_BOT_DAILY_MAX_LOSS_KRW", "300000") or 300000)
BOX_BOT_DAILY_MAX_TRADES = int(os.getenv("BOX_BOT_DAILY_MAX_TRADES", "10") or 10)
BOX_RT_REBUILD_WATCHLIST_ON_EXIT = os.getenv("BOX_RT_REBUILD_WATCHLIST_ON_EXIT", "true").lower() in {"1", "true", "yes", "on"}
BOX_RT_EXCLUDE_DAILY_LOSERS_FROM_RANKING = os.getenv("BOX_RT_EXCLUDE_DAILY_LOSERS_FROM_RANKING", "true").lower() in {"1", "true", "yes", "on"}
BOX_BOT_SIZE_0900_0930 = float(os.getenv("BOX_BOT_SIZE_0900_0930", "0.5") or 0.5)
BOX_BOT_SIZE_0930_1030 = float(os.getenv("BOX_BOT_SIZE_0930_1030", "1.0") or 1.0)
BOX_BOT_SIZE_1030_1300 = float(os.getenv("BOX_BOT_SIZE_1030_1300", "0.35") or 0.35)
BOX_BOT_SIZE_1300_1430 = float(os.getenv("BOX_BOT_SIZE_1300_1430", "1.0") or 1.0)
BOX_BOT_SIZE_1430_1500 = float(os.getenv("BOX_BOT_SIZE_1430_1500", "0.0") or 0.0)
BOX_BOT_CONSECUTIVE_LOSS_LIMIT = int(os.getenv("BOX_BOT_CONSECUTIVE_LOSS_LIMIT", "3") or 3)
BOX_BOT_CONSECUTIVE_LOSS_COOLDOWN_SEC = int(os.getenv("BOX_BOT_CONSECUTIVE_LOSS_COOLDOWN_SEC", "3600") or 3600)
BOX_BOT_POST_CIRCUIT_FIRST_SIZE_MULTIPLIER = float(os.getenv("BOX_BOT_POST_CIRCUIT_FIRST_SIZE_MULTIPLIER", "0.5") or 0.5)
BOX_RT_RUNTIME_PATH = Path(__file__).parents[1] / "data" / "realtime_runtime.json"
TRADE_JOURNAL_DIR = Path(__file__).parents[1] / "journal" / "live_trade_journal"
_LAST_ALERT_TS: dict[str, float] = defaultdict(float)
_RECENT_ENTRY_TIMES: list[datetime] = []
_SYMBOL_REENTRY_BLOCKS: dict[str, tuple[datetime, str]] = {}
_SYMBOL_DAILY_STATE: dict[str, dict[str, int | float | str]] = {}
_BOX_BREAKOUT_ATTEMPTS: dict[str, set[str]] = defaultdict(set)
_CIRCUIT_BREAKER_UNTIL: datetime | None = None
_POST_CIRCUIT_RECOVERY_PENDING = False
_LAST_UNIVERSE_SNAPSHOT: dict[str, object] = {}
UNIVERSE_MODE = os.getenv("BOX_BOT_UNIVERSE_MODE", "dynamic").strip().lower() or "dynamic"
UNIVERSE_TOP_N = int(os.getenv("BOX_BOT_UNIVERSE_TOP_N", "100") or 100)
UNIVERSE_MIN_CHANGE_PCT = float(os.getenv("BOX_BOT_UNIVERSE_MIN_CHANGE_PCT", "0.0") or 0.0)
UNIVERSE_SECTOR_BOOST_ENABLED = os.getenv("BOX_BOT_UNIVERSE_SECTOR_BOOST_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
UNIVERSE_REFRESH_SEC = int(os.getenv("BOX_BOT_UNIVERSE_REFRESH_SEC", "300") or 300)
UNIVERSE_KIS_MARKET = os.getenv("BOX_BOT_UNIVERSE_KIS_MARKET", "0001").strip() or "0001"
UNIVERSE_MIN_TURNOVER_KRW = float(os.getenv("BOX_BOT_UNIVERSE_MIN_TURNOVER_KRW", "20000000000") or 20000000000)
UNIVERSE_POWER_SLOTS = int(os.getenv("BOX_BOT_UNIVERSE_POWER_SLOTS", "5") or 5)
UNIVERSE_POWER_MIN_TURNOVER_KRW = float(os.getenv("BOX_BOT_UNIVERSE_POWER_MIN_TURNOVER_KRW", "5000000000") or 5000000000)
UNIVERSE_POWER_MIN_MARKET_CAP_KRW = float(os.getenv("BOX_BOT_UNIVERSE_POWER_MIN_MARKET_CAP_KRW", "300000000000") or 300000000000)
UNIVERSE_POWER_MIN_CHANGE_PCT = float(os.getenv("BOX_BOT_UNIVERSE_POWER_MIN_CHANGE_PCT", "3.0") or 3.0)
TREND_REBREAK_ENABLED = os.getenv("BOX_BOT_TREND_REBREAK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
TREND_REBREAK_MIN_DAY_PCT = float(os.getenv("BOX_BOT_TREND_REBREAK_MIN_DAY_PCT", "3.0") or 3.0)
TREND_REBREAK_MIN_SWING_PCT = float(os.getenv("BOX_BOT_TREND_REBREAK_MIN_SWING_PCT", "2.0") or 2.0)
TREND_REBREAK_MIN_VOL_RATIO = float(os.getenv("BOX_BOT_TREND_REBREAK_MIN_VOL_RATIO", "1.3") or 1.3)
TREND_REBREAK_BREAKOUT_BUF_PCT = float(os.getenv("BOX_BOT_TREND_REBREAK_BREAKOUT_BUF_PCT", "0.05") or 0.05)

KR_BOX_BOT_UNIVERSE_KOSPI = [
    "005930", "000660", "005380", "000270", "068270", "035420", "005490",
    "042660", "066570", "003550", "012330", "012450", "267250", "028260",
    "032830", "017670", "009540", "078930", "015760", "055550", "024110",
    "006800", "071050", "016360", "005830", "047810", "272210", "064350",
    "443060", "064400", "079550", "042700", "357780", "307950", "007660",
    "010120", "018260", "141080", "214450", "214150", "145020",
    "237690", "003230", "030200", "000720", "028050", "021240", "267260",
    "277810", "034730", "010130", "058470", "039030", "064760", "034020",
    "032640", "241560", "000990", "005290", "058610", "319660",
    "003670", "247540", "005935",
]

ETF_NAME_PREFIXES = (
    "KODEX", "TIGER", "KOSEF", "RISE", "ACE", "SOL", "HANARO", "ARIRANG", "PLUS",
    "TIMEFOLIO", "KBSTAR",
)

SECTOR_KEYWORDS = {
    "semiconductor": ("반도체", "하이닉스", "sk hynix", "db하이텍", "한미반도체", "이오테크", "리노공업", "원익", "테크", "전자", "sfa", "주성"),
    "bio_healthcare": ("바이오", "제약", "pharm", "셀트리온", "리가켐", "알테오젠", "한미약품", "유한양행", "메드", "헬스", "care", "팜"),
    "finance": ("금융", "은행", "손해보험", "생명", "증권", "카드", "지주", "db손해", "삼성생명", "미래에셋", "기업은행", "kb", "신한", "하나"),
    "shipbuilding_defense": ("조선", "중공업", "현대마린", "한화오션", "방산", "디펜스", "항공우주", "k2", "lignex", "현대로템"),
    "energy_power": ("에너지", "전력", "가스", "전선", "변압기", "원전", "두산에너", "효성중공업", "ls electric", "hd현대일렉", "한전"),
    "autos_battery": ("자동차", "모비스", "기아", "현대차", "배터리", "에코프로", "포스코퓨처", "삼성sdi", "lg에너지", "엘앤에프"),
    "steel_materials": ("철강", "홀딩스", "금속", "화학", "롯데케미칼", "포스코", "고려아연", "lg화학", "skc"),
    "retail_consumer": ("화장품", "아모레", "클래시스", "f&f", "호텔", "여행", "유통", "식품", "오리온", "농심"),
}


def _to_float(value, default: float = 0.0) -> float:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text:
            return default
        return float(text.lstrip("+"))
    except Exception:
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text:
            return default
        return int(float(text.lstrip("+")))
    except Exception:
        return default


def _stock_row_metrics(row: dict) -> dict:
    close = _to_float(
        row.get("cur_prc")
        or row.get("close")
        or row.get("price")
        or row.get("last")
        or row.get("lastPrice")
    )
    volume = _to_int(
        row.get("trde_qty")
        or row.get("volume")
        or row.get("acc_trde_qty")
    )
    listed_shares = _to_int(row.get("listCount"))
    turnover = _to_float(
        row.get("trde_amt")
        or row.get("acc_trde_amt")
        or row.get("deal_amt")
        or row.get("trading_value")
    )
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    market_cap_proxy = close * listed_shares if close > 0 and listed_shares > 0 else 0.0
    if turnover <= 0 and market_cap_proxy > 0:
        turnover = market_cap_proxy
    change_pct = _to_float(
        row.get("flu_rt")
        or row.get("rate")
        or row.get("chg_rt")
        or row.get("change_rate")
    )
    return {
        "close": close,
        "volume": volume,
        "listed_shares": listed_shares,
        "turnover": turnover,
        "market_cap_proxy": market_cap_proxy,
        "change_pct": change_pct,
    }


def _classify_sector(name: str, sector_hint: str = "") -> str:
    normalized = f"{(sector_hint or '').strip()} {(name or '').strip()}".lower()
    if not normalized:
        return "other"
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(keyword.lower() in normalized for keyword in keywords):
            return sector
    return "other"


def _dynamic_universe(kospi_all: list[dict], top: int) -> list[dict]:
    ranked: list[dict] = []
    excluded: list[tuple[str, str, str]] = []
    for row in kospi_all:
        code = row.get("code", "")
        name = row.get("name", code)
        if not code:
            continue
        blocked, reason = _is_excluded_name(name)
        if blocked:
            excluded.append((code, name, reason))
            continue
        metrics = _stock_row_metrics(row)
        if metrics["close"] <= 0 or metrics["turnover"] <= 0:
            continue
        if metrics["change_pct"] < UNIVERSE_MIN_CHANGE_PCT:
            continue
        sector_hint = row.get("upName", "")
        sector = _classify_sector(name, sector_hint)
        ranked.append({
            "code": code,
            "name": name,
            "close": metrics["close"],
            "volume": metrics["volume"],
            "turnover": metrics["turnover"],
            "market_cap_proxy": metrics["market_cap_proxy"],
            "change_pct": metrics["change_pct"],
            "sector": sector,
            "sector_hint": sector_hint,
        })

    sector_scores: dict[str, float] = {}
    if UNIVERSE_SECTOR_BOOST_ENABLED:
        sector_buckets: dict[str, list[dict]] = defaultdict(list)
        for item in ranked:
            sector_buckets[item["sector"]].append(item)
        for sector, items in sector_buckets.items():
            if sector == "other":
                sector_scores[sector] = 0.0
                continue
            positive_count = sum(1 for item in items if item["change_pct"] > 0)
            avg_change = sum(item["change_pct"] for item in items) / max(len(items), 1)
            total_turnover = sum(item["turnover"] for item in items)
            sector_scores[sector] = (
                positive_count * 0.8
                + avg_change * 1.2
                + min(total_turnover / 1_000_000_000_000, 6.0)
            )
    for item in ranked:
        item["sector_score"] = sector_scores.get(item["sector"], 0.0)

    ranked.sort(
        key=lambda item: (
            1 if item["change_pct"] > 0 else 0,
            item.get("sector_score", 0.0),
            item["turnover"],
            item.get("market_cap_proxy", 0.0),
            item["volume"],
            item["change_pct"],
        ),
        reverse=True,
    )
    selected = ranked[:max(1, min(top, UNIVERSE_TOP_N))]
    logger.info(
        "스캔 대상: 한국 박스봇 동적 유니버스 %d종목 (mode=%s, min_change=%.2f%%, sector_boost=%s)",
        len(selected),
        UNIVERSE_MODE,
        UNIVERSE_MIN_CHANGE_PCT,
        "on" if UNIVERSE_SECTOR_BOOST_ENABLED else "off",
    )
    if selected:
        preview = ", ".join(
            f"{item['name']}({item['code']})/{item['sector_hint'] or item['sector']}/섹터점수{item.get('sector_score', 0.0):.1f}/기준값{int(item['turnover']):,}/등락{item['change_pct']:+.2f}%"
            for item in selected[:12]
        )
        logger.info("동적 유니버스 상위: %s", preview)
    if excluded:
        logger.info("유니버스 제외 %d종목: %s", len(excluded), ", ".join(f"{n}({c})/{r}" for c, n, r in excluded[:12]))
    return [{"code": item["code"], "name": item["name"]} for item in selected]


def _is_excluded_name(name: str) -> tuple[bool, str]:
    normalized = (name or "").strip()
    if not normalized:
        return False, ""
    if "스팩" in normalized:
        return True, "spac"
    if normalized.startswith(ETF_NAME_PREFIXES) or " ETF" in normalized or normalized.endswith("ETF"):
        return True, "etf"
    if "ETN" in normalized:
        return True, "etn"
    if normalized.endswith("리츠"):
        return True, "reit"
    if normalized.endswith("우") or "우B" in normalized or "우(" in normalized or "(전환)" in normalized:
        return True, "preferred"
    return False, ""


def _send(msg: str) -> None:
    if not TELEGRAM_TOKEN:
        return
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                timeout=15,
            )
            res.raise_for_status()
            return
        except Exception as e:
            last_error = e
            if attempt < 3:
                time.sleep(1.2 * attempt)
    logger.warning("텔레그램 전송 실패: %s", last_error)


def _send_once_per_key(key: str, msg: str, *, cooldown_sec: int = ALERT_COOLDOWN_SEC) -> None:
    now_ts = time.time()
    if now_ts - _LAST_ALERT_TS[key] < cooldown_sec:
        return
    _LAST_ALERT_TS[key] = now_ts
    _send(msg)


def _is_live_broker(broker: KisDomesticBroker | None) -> bool:
    return bool(broker and not getattr(broker, "is_mock", True))


def _mode_label(broker: KisDomesticBroker | None) -> str:
    return "실전" if _is_live_broker(broker) else "모의"


def _active_symbol_reentry_block(code: str, ts: str) -> tuple[bool, str]:
    blocked = _SYMBOL_REENTRY_BLOCKS.get(code)
    if not blocked:
        return False, ""
    block_until, block_reason = blocked
    now_dt = _parse_kst_ts(ts)
    if now_dt >= block_until:
        _SYMBOL_REENTRY_BLOCKS.pop(code, None)
        return False, ""
    wait_sec = max(int((block_until - now_dt).total_seconds()), 0)
    wait_min = max(1, (wait_sec + 59) // 60)
    return True, f"{block_reason}:{wait_min}m"


def _set_symbol_reentry_block(code: str, ts: str, reason: str, pnl_krw: int | float) -> None:
    if pnl_krw >= 0:
        return
    cooldown_sec = BOX_RT_LOSS_REENTRY_COOLDOWN_SEC
    if reason == "follow_through_fail":
        cooldown_sec = max(cooldown_sec, BOX_RT_FOLLOW_FAIL_REENTRY_COOLDOWN_SEC)
    if cooldown_sec <= 0:
        return
    until_dt = _parse_kst_ts(ts) + timedelta(seconds=cooldown_sec)
    _SYMBOL_REENTRY_BLOCKS[code] = (until_dt, f"loss_reentry_cooldown:{reason}")


def _rebuild_symbol_daily_state(engine: StrategyStateEngine, date_str: str) -> None:
    global _SYMBOL_DAILY_STATE
    rebuilt: dict[str, dict[str, int | float | str]] = {}
    for trade in engine.trades:
        if str(trade.entry_ts)[:8] != date_str:
            continue
        row = rebuilt.setdefault(
            trade.code,
            {"name": trade.name, "attempts": 0, "losses": 0, "realized_pnl": 0},
        )
        row["name"] = trade.name
        row["attempts"] = int(row["attempts"]) + 1
        row["realized_pnl"] = int(row["realized_pnl"]) + int(trade.pnl_krw)
        if trade.pnl_krw < 0:
            row["losses"] = int(row["losses"]) + 1
    for code, pos in engine.positions.items():
        if str(pos.entry_ts)[:8] != date_str:
            continue
        row = rebuilt.setdefault(
            code,
            {"name": pos.name, "attempts": 0, "losses": 0, "realized_pnl": 0},
        )
        row["name"] = pos.name
        row["attempts"] = int(row["attempts"]) + 1
    _SYMBOL_DAILY_STATE = rebuilt


def _record_symbol_entry(code: str, name: str) -> None:
    row = _SYMBOL_DAILY_STATE.setdefault(
        code,
        {"name": name, "attempts": 0, "losses": 0, "realized_pnl": 0},
    )
    row["name"] = name
    row["attempts"] = int(row["attempts"]) + 1


def _record_symbol_exit(trade) -> None:
    row = _SYMBOL_DAILY_STATE.setdefault(
        trade.code,
        {"name": trade.name, "attempts": 0, "losses": 0, "realized_pnl": 0},
    )
    row["name"] = trade.name
    row["realized_pnl"] = int(row["realized_pnl"]) + int(trade.pnl_krw)
    if trade.pnl_krw < 0:
        row["losses"] = int(row["losses"]) + 1


def _remember_universe_snapshot(stocks: list[dict]) -> None:
    global _LAST_UNIVERSE_SNAPSHOT
    leaders = []
    for item in stocks[:10]:
        leaders.append(
            {
                "code": item.get("code", ""),
                "name": item.get("name", item.get("code", "")),
                "change_pct": round(float(item.get("change_pct", 0.0) or 0.0), 2),
                "leader_score": round(float(item.get("leader_score", 0.0) or 0.0), 2),
                "trade_intensity": round(float(item.get("volume_power_score", 0.0) or 0.0), 1),
            }
        )
    _LAST_UNIVERSE_SNAPSHOT = {
        "captured_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "universe_mode": UNIVERSE_MODE,
        "count": len(stocks),
        "leaders": leaders,
    }


def _journal_paths(date_str: str) -> tuple[Path, Path]:
    TRADE_JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return (
        TRADE_JOURNAL_DIR / f"{date_str}_trade_journal.jsonl",
        TRADE_JOURNAL_DIR / f"{date_str}_trade_journal.md",
    )


def _journal_market_context(ts: str) -> dict:
    hhmm = ts[8:12] if len(ts) >= 12 else datetime.now(KST).strftime("%H%M")
    return {
        "captured_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "time_bucket": hhmm,
        "time_policy": _time_policy(hhmm),
        "size_multiplier": round(_time_bucket_size_multiplier(hhmm), 3),
        "daily_realized_pnl_krw": _daily_realized_pnl(),
        "daily_attempts": _daily_trade_attempts(),
        "daily_blacklist_codes": sorted(_daily_blacklist_codes()),
        "universe": dict(_LAST_UNIVERSE_SNAPSHOT or {}),
    }


def _append_trade_journal_event(date_str: str, payload: dict) -> None:
    jsonl_path, _ = _journal_paths(date_str)
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    _rewrite_trade_journal_markdown(date_str)


def _rewrite_trade_journal_markdown(date_str: str) -> None:
    jsonl_path, md_path = _journal_paths(date_str)
    if not jsonl_path.exists():
        return
    events = []
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    if not events:
        return

    events.sort(key=lambda item: (item.get("ts", ""), item.get("event", "")))
    entry_count = sum(1 for item in events if item.get("event") == "entry")
    exit_count = sum(1 for item in events if item.get("event") == "exit")
    lines = [
        f"# 박스봇 실전 매매일지 {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
        "",
        f"- 생성시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}",
        f"- 이벤트 수: {len(events)}",
        f"- 진입 {entry_count}건 / 청산 {exit_count}건",
        "",
        "## 거래 이벤트",
        "",
    ]
    for idx, item in enumerate(events, start=1):
        symbol = f"{item.get('name', '-') }({item.get('code', '-')})"
        lines.append(f"### {idx}. {str(item.get('event', '')).upper()} {symbol} {item.get('hhmm', '--:--')}")
        lines.append(f"- trade_id: {item.get('trade_id', '-')}")
        lines.append(f"- 시각: {item.get('ts_kst', item.get('ts', '-'))}")
        if item.get("event") == "entry":
            lines.append(f"- 진입가/수량: {int(item.get('price', 0)):,}원 / {int(item.get('qty', 0))}주")
            lines.append(f"- 전략: {item.get('strategy_type', '-')}")
            lines.append(f"- 박스: {item.get('box_grade', '-')}급, 폭 {float(item.get('box_height_pct', 0.0)):.2f}%, 길이 {int(item.get('box_length', 0))}봉")
            lines.append(f"- 진입근거: {item.get('entry_signal_summary', '-')}")
        else:
            lines.append(f"- 청산가/수익: {int(item.get('price', 0)):,}원 / {_signed_krw(item.get('pnl_krw', 0))} ({float(item.get('pnl_pct', 0.0)):+.2f}%)")
            lines.append(f"- 청산사유: {item.get('exit_reason', '-')}")
            lines.append(f"- 보유시간: {item.get('held_minutes', 0)}분")
            lines.append(f"- 청산판단: {item.get('exit_signal_summary', '-')}")
        market = item.get("market_context") or {}
        universe = market.get("universe") or {}
        leaders = universe.get("leaders") or []
        if leaders:
            leader_summary = ", ".join(
                f"{row.get('name', row.get('code', '-'))}({row.get('change_pct', 0.0):+.2f}%)"
                for row in leaders[:5]
            )
            lines.append(f"- 시장상황: 유니버스 {universe.get('count', 0)}종목, 상위 {leader_summary}")
        lines.append(f"- 장중상태: 시간대 {market.get('time_bucket', '-')}, 정책 {market.get('time_policy', '-')}, 일손익 {_signed_krw(market.get('daily_realized_pnl_krw', 0))}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")


def _build_entry_context(*, code: str, name: str, ts: str, price: float, qty: int, strategy_type: str, size_multiplier: float, box_info: dict, signal_summary: str, extra: dict | None = None) -> dict:
    return {
        "trade_id": f"{code}-{ts}",
        "entry_signal_summary": signal_summary,
        "strategy_type": strategy_type,
        "size_multiplier": round(size_multiplier, 3),
        "box_grade": box_info.get("box_grade", "C"),
        "box_height_pct": round(float(box_info.get("box_height_pct", 0.0) or 0.0), 3),
        "box_length": int(box_info.get("box_length", 0) or 0),
        "box_high": float(box_info.get("box_high", 0.0) or 0.0),
        "box_low": float(box_info.get("box_low", 0.0) or 0.0),
        "preferred_box": bool(box_info.get("preferred_box", False)),
        "is_rising_lows": bool(box_info.get("is_rising_lows", False)),
        "daily_pass": bool(box_info.get("daily_pass", False)),
        "rank_meta": extra or {},
        "market_context": _journal_market_context(ts),
    }


def _write_trade_entry_journal(code: str, name: str, ts: str, price: float, qty: int, entry_context: dict) -> None:
    _append_trade_journal_event(
        ts[:8],
        {
            "event": "entry",
            "trade_id": entry_context.get("trade_id", f"{code}-{ts}"),
            "code": code,
            "name": name,
            "ts": ts,
            "ts_kst": _parse_kst_ts(ts).strftime("%Y-%m-%d %H:%M:%S KST"),
            "hhmm": ts[8:12],
            "price": price,
            "qty": qty,
            "strategy_type": entry_context.get("strategy_type", "-"),
            "box_grade": entry_context.get("box_grade", "C"),
            "box_height_pct": entry_context.get("box_height_pct", 0.0),
            "box_length": entry_context.get("box_length", 0),
            "entry_signal_summary": entry_context.get("entry_signal_summary", "-"),
            "market_context": entry_context.get("market_context", {}),
        },
    )


def _build_exit_context(pos, *, exit_price: float, exit_ts: str, exit_reason: str, source: str) -> dict:
    held_minutes = 0
    try:
        held_minutes = max(int((_parse_kst_ts(exit_ts) - _parse_kst_ts(pos.entry_ts)).total_seconds() // 60), 0)
    except Exception:
        held_minutes = 0
    box_top = float(pos.box_high or pos.entry_price or 0.0)
    gap_from_box_top_pct = ((exit_price / box_top) - 1.0) * 100 if box_top > 0 else 0.0
    peak_pnl_pct = ((float(pos.peak_price or pos.entry_price) / pos.entry_price) - 1.0) * 100 if pos.entry_price > 0 else 0.0
    return {
        "source": source,
        "held_minutes": held_minutes,
        "peak_price": float(pos.peak_price or pos.entry_price),
        "peak_pnl_pct": round(peak_pnl_pct, 3),
        "gap_from_box_top_pct": round(gap_from_box_top_pct, 3),
        "box_high": float(pos.box_high or 0.0),
        "box_low": float(pos.box_low or 0.0),
        "market_context": _journal_market_context(exit_ts),
        "exit_signal_summary": f"{exit_reason} | peak {peak_pnl_pct:+.2f}% | box_top_gap {gap_from_box_top_pct:+.2f}%",
    }


def _write_trade_exit_journal(trade) -> None:
    entry_context = dict(getattr(trade, "entry_context", {}) or {})
    exit_context = dict(getattr(trade, "exit_context", {}) or {})
    _append_trade_journal_event(
        trade.exit_ts[:8],
        {
            "event": "exit",
            "trade_id": entry_context.get("trade_id", f"{trade.code}-{trade.entry_ts}"),
            "code": trade.code,
            "name": trade.name,
            "ts": trade.exit_ts,
            "ts_kst": _parse_kst_ts(trade.exit_ts).strftime("%Y-%m-%d %H:%M:%S KST"),
            "hhmm": trade.exit_ts[8:12],
            "price": trade.exit_price,
            "qty": trade.qty,
            "pnl_krw": trade.pnl_krw,
            "pnl_pct": trade.pnl_pct,
            "exit_reason": trade.exit_reason,
            "held_minutes": exit_context.get("held_minutes", 0),
            "exit_signal_summary": exit_context.get("exit_signal_summary", trade.exit_reason),
            "market_context": exit_context.get("market_context", {}),
        },
    )


def _symbol_daily_block_reason(code: str) -> str:
    row = _SYMBOL_DAILY_STATE.get(code) or {}
    attempts = int(row.get("attempts", 0) or 0)
    losses = int(row.get("losses", 0) or 0)
    if attempts >= BOX_RT_MAX_ATTEMPTS_PER_SYMBOL_PER_DAY:
        return f"symbol_daily_attempt_cap:{attempts}"
    if losses >= BOX_RT_MAX_LOSSES_PER_SYMBOL_PER_DAY:
        return f"symbol_daily_loss_cap:{losses}"
    return ""


def _daily_trade_attempts() -> int:
    return sum(int(row.get("attempts", 0) or 0) for row in _SYMBOL_DAILY_STATE.values())


def _daily_realized_pnl() -> int:
    return sum(int(row.get("realized_pnl", 0) or 0) for row in _SYMBOL_DAILY_STATE.values())


def _daily_blacklist_codes() -> set[str]:
    if not BOX_RT_EXCLUDE_DAILY_LOSERS_FROM_RANKING:
        return set()
    return {
        code
        for code, row in _SYMBOL_DAILY_STATE.items()
        if int(row.get("losses", 0) or 0) >= BOX_RT_MAX_LOSSES_PER_SYMBOL_PER_DAY
    }


def _daily_buy_block_reason(date_str: str) -> str:
    attempts = _daily_trade_attempts()
    realized_pnl = _daily_realized_pnl()
    if BOX_BOT_DAILY_MAX_LOSS_KRW > 0 and realized_pnl <= -BOX_BOT_DAILY_MAX_LOSS_KRW:
        return f"daily_loss_limit:{realized_pnl}"
    if BOX_BOT_DAILY_MAX_TRADES > 0 and attempts >= BOX_BOT_DAILY_MAX_TRADES:
        return f"daily_trade_limit:{attempts}"
    return ""


def _time_bucket_size_multiplier(hhmm: str) -> float:
    if "0900" <= hhmm < "0930":
        return BOX_BOT_SIZE_0900_0930
    if "0930" <= hhmm < "1030":
        return BOX_BOT_SIZE_0930_1030
    if "1030" <= hhmm < "1300":
        return BOX_BOT_SIZE_1030_1300
    if "1300" <= hhmm < "1430":
        return BOX_BOT_SIZE_1300_1430
    if "1430" <= hhmm < "1500":
        return BOX_BOT_SIZE_1430_1500
    return 0.0


def _rebuild_loss_circuit_state(engine: StrategyStateEngine, date_str: str) -> None:
    global _CIRCUIT_BREAKER_UNTIL, _POST_CIRCUIT_RECOVERY_PENDING
    _CIRCUIT_BREAKER_UNTIL = None
    _POST_CIRCUIT_RECOVERY_PENDING = False
    streak = 0
    until = None
    for trade in sorted((t for t in engine.trades if t.entry_ts[:8] == date_str), key=lambda t: t.exit_ts):
        if trade.pnl_krw < 0:
            streak += 1
        else:
            streak = 0
        if streak >= BOX_BOT_CONSECUTIVE_LOSS_LIMIT:
            until = _parse_kst_ts(trade.exit_ts) + timedelta(seconds=BOX_BOT_CONSECUTIVE_LOSS_COOLDOWN_SEC)
            streak = 0
    if until and datetime.now(KST) < until:
        _CIRCUIT_BREAKER_UNTIL = until
        _POST_CIRCUIT_RECOVERY_PENDING = True


def _loss_circuit_reason(ts: str) -> str:
    if not _CIRCUIT_BREAKER_UNTIL:
        return ""
    now_dt = _parse_kst_ts(ts)
    if now_dt >= _CIRCUIT_BREAKER_UNTIL:
        return ""
    wait_min = max(1, int((_CIRCUIT_BREAKER_UNTIL - now_dt).total_seconds() + 59) // 60)
    return f"loss_circuit_breaker:{wait_min}m"


def _size_multiplier_for_entry(ts: str) -> tuple[float, str]:
    hhmm = ts[8:12] if len(ts) >= 12 else datetime.now(KST).strftime("%H%M")
    base = _time_bucket_size_multiplier(hhmm)
    if base <= 0:
        return 0.0, f"time_bucket_disabled:{hhmm}"
    if _POST_CIRCUIT_RECOVERY_PENDING:
        return base * BOX_BOT_POST_CIRCUIT_FIRST_SIZE_MULTIPLIER, "post_circuit_reduced"
    return base, "time_bucket"


def _maybe_trigger_loss_circuit(trade, engine: StrategyStateEngine) -> None:
    global _CIRCUIT_BREAKER_UNTIL, _POST_CIRCUIT_RECOVERY_PENDING
    today = [t for t in engine.trades if t.entry_ts[:8] == trade.entry_ts[:8]]
    recent = sorted(today, key=lambda t: t.exit_ts)[-BOX_BOT_CONSECUTIVE_LOSS_LIMIT:]
    if len(recent) < BOX_BOT_CONSECUTIVE_LOSS_LIMIT:
        return
    if any(t.pnl_krw >= 0 for t in recent):
        return
    _CIRCUIT_BREAKER_UNTIL = _parse_kst_ts(trade.exit_ts) + timedelta(seconds=BOX_BOT_CONSECUTIVE_LOSS_COOLDOWN_SEC)
    _POST_CIRCUIT_RECOVERY_PENDING = True
    _send_once_per_key(
        f"loss_circuit:{trade.entry_ts[:8]}:{trade.exit_ts}",
        f"🛑 *박스봇 연속 손실 서킷브레이커 발동* {trade.entry_ts[:8]}\n"
        f"  최근 {BOX_BOT_CONSECUTIVE_LOSS_LIMIT}연속 손실\n"
        f"  {BOX_BOT_CONSECUTIVE_LOSS_COOLDOWN_SEC // 60}분 신규 진입 중단\n"
        f"  이후 첫 진입은 {int(BOX_BOT_POST_CIRCUIT_FIRST_SIZE_MULTIPLIER * 100)}% 사이즈\n"
        f"  {_stats_line(engine)}",
        cooldown_sec=3600,
    )


def _consume_post_circuit_recovery() -> None:
    global _POST_CIRCUIT_RECOVERY_PENDING, _CIRCUIT_BREAKER_UNTIL
    if _POST_CIRCUIT_RECOVERY_PENDING:
        _POST_CIRCUIT_RECOVERY_PENDING = False
        _CIRCUIT_BREAKER_UNTIL = None


def _send_daily_limit_alert_once(date_str: str, reason: str, engine: StrategyStateEngine) -> None:
    if reason.startswith("daily_loss_limit:"):
        _send_once_per_key(
            f"daily_loss_limit:{date_str}",
            f"🛑 *박스봇 일일 손실 한도 도달* {date_str}\n"
            f"  실현손익 {_signed_krw(_daily_realized_pnl())}\n"
            f"  신규 매수 중단 / 청산만 유지\n"
            f"  {_stats_line(engine)}",
            cooldown_sec=86400,
        )
    elif reason.startswith("daily_trade_limit:"):
        _send_once_per_key(
            f"daily_trade_limit:{date_str}",
            f"🛑 *박스봇 일일 거래 한도 도달* {date_str}\n"
            f"  진입횟수 {_daily_trade_attempts()} / 한도 {BOX_BOT_DAILY_MAX_TRADES}\n"
            f"  신규 매수 중단 / 청산만 유지\n"
            f"  {_stats_line(engine)}",
            cooldown_sec=86400,
        )


def _box_breakout_key(code: str, box_high: float, box_low: float, box_length: int, box_grade: str, date_str: str) -> str:
    return f"{date_str}:{code}:{round(box_high, 1)}:{round(box_low, 1)}:{box_length}:{box_grade}"


def _box_breakout_attempted(code: str, box_high: float, box_low: float, box_length: int, box_grade: str, date_str: str) -> bool:
    return _box_breakout_key(code, box_high, box_low, box_length, box_grade, date_str) in _BOX_BREAKOUT_ATTEMPTS.get(code, set())


def _record_box_breakout_attempt(code: str, box_high: float, box_low: float, box_length: int, box_grade: str, date_str: str) -> None:
    _BOX_BREAKOUT_ATTEMPTS[code].add(_box_breakout_key(code, box_high, box_low, box_length, box_grade, date_str))


def _signed_krw(value: float | int) -> str:
    value_int = int(round(float(value)))
    return f"{value_int:+,}원"


def _stats_line(engine: StrategyStateEngine) -> str:
    stats = engine.cumulative_stats()
    return (
        f"누적손익 {_signed_krw(stats['net_pnl'])} | "
        f"누적이익 {_signed_krw(stats['realized_profit'])} | "
        f"누적손실 -{int(stats['realized_loss']):,}원 | "
        f"체결 {stats['trade_count']} | 승률 {stats['win_rate']}% | "
        f"수수료 {int(stats.get('total_fees', 0)):,}원 | "
        f"보유 {stats['positions']} | 현금 {int(stats['cash']):,}원"
    )


def _heartbeat_message(engine: StrategyStateEngine) -> str:
    now = datetime.now(KST)
    return (
        f"💓 박스봇 정상가동\n"
        f"  시각 {now:%m/%d %H:%M KST}\n"
        f"  {_stats_line(engine)}"
    )


def _now_hm() -> tuple[int, int]:
    now = datetime.now(KST)
    return now.hour, now.minute


def _is_market_hours() -> bool:
    h, m = _now_hm()
    if not 0 <= datetime.now(KST).weekday() <= 4:
        return False
    return (h, m) >= MARKET_OPEN and (h, m) < (15, 20)


def _parse_kst_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=KST)


def _prune_recent_entries(now_dt: datetime) -> None:
    global _RECENT_ENTRY_TIMES
    _RECENT_ENTRY_TIMES[:] = [ts for ts in _RECENT_ENTRY_TIMES if (now_dt - ts).total_seconds() <= 600]


def _is_opening_window(hhmm: str) -> bool:
    return hhmm < OPENING_REQUIRE_PREFERRED_UNTIL


def _candidate_rank(box_info: dict) -> tuple:
    return (
        1 if box_info.get("strategy_type") == "trend_rebreak" else 0,
        {"A": 2, "B": 1, "C": 0}.get(box_info.get("box_grade", "C"), 0),
        1 if box_info.get("preferred_box", False) else 0,
        float(box_info.get("breakout_close_pct", 0.0)),
        float(box_info.get("breakout_body_ratio", 0.0)),
        float(box_info.get("box_height_pct", 0.0)),
        int(box_info.get("box_length", 0)),
    )


def _watchlist_rank(candidate: dict) -> tuple:
    return (
        {"A": 2, "B": 1, "C": 0}.get(candidate.get("box_grade", "C"), 0),
        1 if candidate.get("preferred_box", False) else 0,
        -float(candidate.get("distance_pct", 99.0)),
        float(candidate.get("box_height_pct", 0.0)),
        int(candidate.get("box_length", 0)),
    )


def _check_trend_rebreak(candles: list[Candle]) -> tuple[bool, dict]:
    info = {
        "strategy_type": "trend_rebreak",
        "is_valid_box": False,
        "box_height_pct": 0.0,
        "box_length": 0,
        "box_high": 0.0,
        "box_low": 0.0,
        "is_rising_lows": True,
        "breakout_ready": False,
        "breakout_close_pct": 0.0,
        "breakout_body_ratio": 0.0,
        "reject_reason": "",
        "daily_pass": True,
        "preferred_box": False,
    }
    if not TREND_REBREAK_ENABLED:
        info["reject_reason"] = "trend_rebreak_disabled"
        return False, info
    if len(candles) < 20:
        info["reject_reason"] = "insufficient_data"
        return False, info

    latest = candles[-1]
    prev = candles[-2]
    recent = candles[-20:]
    prior_window = candles[-7:-1]
    if len(prior_window) < 3:
        info["reject_reason"] = "insufficient_data"
        return False, info

    breakout_high = max(c.high for c in prior_window)
    pullback_low = min(c.low for c in candles[-10:-1])
    swing_low = min(c.low for c in recent[:-1])
    day_change_pct = ((latest.close / recent[0].open) - 1.0) * 100 if recent[0].open > 0 else 0.0
    swing_pct = ((breakout_high / swing_low) - 1.0) * 100 if swing_low > 0 else 0.0
    breakout_close_pct = ((latest.close / breakout_high) - 1.0) * 100 if breakout_high > 0 else 0.0
    avg_vol = sum(c.volume for c in candles[-11:-1]) / 10
    vol_ratio = latest.volume / avg_vol if avg_vol > 0 else 0.0
    body_ratio = latest.body / max(latest.range, 1e-9)
    sma5 = sum(c.close for c in candles[-5:]) / 5
    sma20 = sum(c.close for c in candles[-20:]) / 20
    retrace_pct = ((breakout_high / pullback_low) - 1.0) * 100 if pullback_low > 0 else 0.0

    info["box_high"] = breakout_high
    info["box_low"] = pullback_low
    info["box_length"] = len(prior_window)
    info["box_height_pct"] = round(retrace_pct, 3)
    info["breakout_close_pct"] = round(breakout_close_pct, 3)
    info["breakout_body_ratio"] = round(body_ratio, 3)
    info["preferred_box"] = True

    if day_change_pct < TREND_REBREAK_MIN_DAY_PCT:
        info["reject_reason"] = "trend_day_change_too_small"
        return False, info
    if swing_pct < TREND_REBREAK_MIN_SWING_PCT:
        info["reject_reason"] = "trend_swing_too_small"
        return False, info
    if latest.close <= breakout_high * (1.0 + TREND_REBREAK_BREAKOUT_BUF_PCT / 100.0):
        info["reject_reason"] = "trend_rebreak_not_confirmed"
        return False, info
    if vol_ratio < TREND_REBREAK_MIN_VOL_RATIO:
        info["reject_reason"] = "trend_volume_too_weak"
        return False, info
    if body_ratio < 0.45:
        info["reject_reason"] = "trend_body_too_weak"
        return False, info
    if latest.close <= prev.close:
        info["reject_reason"] = "trend_no_followthrough"
        return False, info
    if sma5 <= sma20:
        info["reject_reason"] = "trend_ma_not_aligned"
        return False, info
    if pullback_low < sma20 * 0.985:
        info["reject_reason"] = "trend_pullback_too_deep"
        return False, info

    info["breakout_ready"] = True
    return True, info


def _time_policy(hhmm: str) -> str:
    if hhmm < NO_BUY_BEFORE:
        return "blocked"
    return "full"


def _today_traded_codes(engine: StrategyStateEngine, date_str: str) -> set[str]:
    return {
        trade.code for trade in engine.trades
        if trade.entry_ts.startswith(date_str)
    }


def _candidate_focus_pool(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    if FOCUS_SINGLE_MODE:
        return candidates[:1]
    top_n = max(1, FOCUS_TOP_CANDIDATES)
    selected = candidates[:1]
    if top_n <= 1 or len(candidates) <= 1:
        return selected

    top = candidates[0]
    top_breakout_close_pct = float(top["box_info"].get("breakout_close_pct", 0.0) or 0.0)
    for candidate in candidates[1:top_n]:
        if not candidate["box_info"].get("preferred_box", False):
            continue
        breakout_close_pct = float(candidate["box_info"].get("breakout_close_pct", 0.0) or 0.0)
        if top_breakout_close_pct <= 0:
            selected.append(candidate)
            continue
        if breakout_close_pct >= top_breakout_close_pct * FOCUS_TOP2_RATIO:
            selected.append(candidate)
    return selected


def _kis_direct_universe(broker: KisDomesticBroker, top: int) -> list[dict]:
    volume_rows = broker.get_volume_rank(market=UNIVERSE_KIS_MARKET)
    power_rows = broker.get_volume_power(market=UNIVERSE_KIS_MARKET)
    foreign_rows = broker.get_foreign_institution_total(market=UNIVERSE_KIS_MARKET)

    merged: dict[str, dict] = {}

    def ensure_item(code: str, name: str) -> dict:
        item = merged.get(code)
        if item is None:
            item = {
                "code": code,
                "name": name or code,
                "close": 0.0,
                "volume": 0,
                "turnover": 0.0,
                "listed_shares": 0,
                "market_cap_proxy": 0.0,
                "change_pct": 0.0,
                "volume_rank_score": 0.0,
                "volume_power_score": 0.0,
                "foreign_score": 0.0,
                "sector": "other",
                "sector_hint": "",
            }
            merged[code] = item
        elif name and (not item.get("name") or item.get("name") == code):
            item["name"] = name
        return item

    for idx, row in enumerate(volume_rows):
        code = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "").strip()
        if not code:
            continue
        name = str(row.get("hts_kor_isnm") or code).strip()
        blocked, _ = _is_excluded_name(name)
        if blocked:
            continue
        item = ensure_item(code, name)
        item["close"] = max(item["close"], _to_float(row.get("stck_prpr")))
        item["volume"] = max(item["volume"], _to_int(row.get("acml_vol")))
        item["turnover"] = max(item["turnover"], _to_float(row.get("acml_tr_pbmn")))
        item["listed_shares"] = max(item["listed_shares"], _to_int(row.get("lstn_stcn")))
        if item["close"] > 0 and item["listed_shares"] > 0:
            item["market_cap_proxy"] = max(item["market_cap_proxy"], item["close"] * item["listed_shares"])
        item["change_pct"] = _to_float(row.get("prdy_ctrt"), item["change_pct"])
        item["volume_rank_score"] = max(item["volume_rank_score"], float(len(volume_rows) - idx))

    for idx, row in enumerate(power_rows):
        code = str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or "").strip()
        if not code:
            continue
        name = str(row.get("hts_kor_isnm") or code).strip()
        blocked, _ = _is_excluded_name(name)
        if blocked:
            continue
        item = ensure_item(code, name)
        item["close"] = max(item["close"], _to_float(row.get("stck_prpr")))
        item["volume"] = max(item["volume"], _to_int(row.get("acml_vol")))
        if item["close"] > 0 and item["volume"] > 0:
            item["turnover"] = max(item["turnover"], item["close"] * item["volume"])
        item["change_pct"] = _to_float(row.get("prdy_ctrt"), item["change_pct"])
        item["volume_power_score"] = max(item["volume_power_score"], _to_float(row.get("tday_rltv")))

    for idx, row in enumerate(foreign_rows):
        code = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "").strip()
        if not code:
            continue
        name = str(row.get("hts_kor_isnm") or code).strip()
        blocked, _ = _is_excluded_name(name)
        if blocked:
            continue
        item = ensure_item(code, name)
        item["close"] = max(item["close"], _to_float(row.get("stck_prpr")))
        item["volume"] = max(item["volume"], _to_int(row.get("acml_vol")))
        if item["close"] > 0 and item["volume"] > 0:
            item["turnover"] = max(item["turnover"], item["close"] * item["volume"])
        item["change_pct"] = _to_float(row.get("prdy_ctrt"), item["change_pct"])
        item["foreign_score"] = max(
            item["foreign_score"],
            _to_float(row.get("frgn_ntby_tr_pbmn")) + _to_float(row.get("orgn_ntby_tr_pbmn")) * 0.5,
        )
        item["foreign_rank_score"] = max(item.get("foreign_rank_score", 0.0), float(len(foreign_rows) - idx))

    ranked: list[dict] = []
    for item in merged.values():
        if item["close"] <= 0:
            continue
        if item["change_pct"] < UNIVERSE_MIN_CHANGE_PCT:
            continue
        item["sector"] = _classify_sector(item["name"], "")
        ranked.append(item)

    sector_scores: dict[str, float] = {}
    if UNIVERSE_SECTOR_BOOST_ENABLED:
        sector_buckets: dict[str, list[dict]] = defaultdict(list)
        for item in ranked:
            sector_buckets[item["sector"]].append(item)
        for sector, items in sector_buckets.items():
            if sector == "other":
                sector_scores[sector] = 0.0
                continue
            positive_count = sum(1 for item in items if item["change_pct"] > 0)
            avg_change = sum(item["change_pct"] for item in items) / max(len(items), 1)
            avg_power = sum(item.get("volume_power_score", 0.0) for item in items) / max(len(items), 1)
            sector_scores[sector] = positive_count * 0.8 + avg_change * 1.0 + min(avg_power / 100.0, 4.0)

    for item in ranked:
        item["sector_score"] = sector_scores.get(item["sector"], 0.0)
        item["leader_score"] = (
            item.get("volume_rank_score", 0.0) * 1.8
            + item.get("volume_power_score", 0.0) * 0.18
            + item.get("foreign_rank_score", 0.0) * 1.5
            + min(item.get("foreign_score", 0.0) / 100_000_000, 20.0)
            + item.get("sector_score", 0.0) * 2.0
            + max(item.get("change_pct", 0.0), 0.0) * 1.5
            + min(item.get("turnover", 0.0) / 20_000_000_000, 25.0)
        )

    ranked.sort(
        key=lambda item: (
            item.get("leader_score", 0.0),
            item.get("volume_rank_score", 0.0),
            item.get("volume_power_score", 0.0),
            item.get("foreign_rank_score", 0.0),
            item.get("change_pct", 0.0),
            item.get("turnover", 0.0),
        ),
        reverse=True,
    )
    target_count = max(1, min(top, UNIVERSE_TOP_N))
    main_pool = [item for item in ranked if item.get("turnover", 0.0) >= UNIVERSE_MIN_TURNOVER_KRW]
    power_pool = []
    for item in ranked:
        if item.get("volume_power_score", 0.0) <= 0:
            continue
        quality_hits = 0
        if item.get("market_cap_proxy", 0.0) >= UNIVERSE_POWER_MIN_MARKET_CAP_KRW:
            quality_hits += 1
        if item.get("turnover", 0.0) >= UNIVERSE_POWER_MIN_TURNOVER_KRW:
            quality_hits += 1
        if item.get("change_pct", 0.0) >= UNIVERSE_POWER_MIN_CHANGE_PCT:
            quality_hits += 1
        item["power_quality_hits"] = quality_hits
        if quality_hits >= 2:
            power_pool.append(item)
    power_pool.sort(
        key=lambda item: (
            item.get("power_quality_hits", 0),
            item.get("volume_power_score", 0.0),
            item.get("change_pct", 0.0),
            item.get("turnover", 0.0),
        ),
        reverse=True,
    )

    selected_map: dict[str, dict] = {}
    reserved_power_slots = min(UNIVERSE_POWER_SLOTS, target_count)
    for item in main_pool[:max(0, target_count - reserved_power_slots)]:
        selected_map[item["code"]] = item

    power_added = 0
    for item in power_pool:
        if power_added >= UNIVERSE_POWER_SLOTS:
            break
        if item["code"] in selected_map:
            continue
        selected_map[item["code"]] = item
        power_added += 1

    for item in main_pool:
        if len(selected_map) >= target_count:
            break
        if item["code"] in selected_map:
            continue
        selected_map[item["code"]] = item

    selected = list(selected_map.values())
    selected.sort(
        key=lambda item: (
            item.get("leader_score", 0.0),
            item.get("volume_power_score", 0.0),
            item.get("turnover", 0.0),
        ),
        reverse=True,
    )
    logger.info(
        "스캔 대상: 한국 박스봇 KIS 직결 유니버스 %d종목 (거래량=%d, 체결강도=%d, 외국인/기관=%d, power_slots=%d/%d)",
        len(selected),
        len(volume_rows),
        len(power_rows),
        len(foreign_rows),
        power_added,
        UNIVERSE_POWER_SLOTS,
    )
    if selected:
        preview = ", ".join(
            f"{item['name']}({item['code']})/점수{item.get('leader_score', 0.0):.1f}/체결강도{item.get('volume_power_score', 0.0):.1f}/등락{item['change_pct']:+.2f}%/품질{item.get('power_quality_hits', 0)}"
            for item in selected[:12]
        )
        logger.info("KIS 유니버스 상위: %s", preview)
    return selected


def _persist_realtime_metrics(payload: dict) -> None:
    BOX_RT_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOX_RT_RUNTIME_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _realtime_confirmation_snapshot(rt_state: BoxRealtimeState, quote) -> dict[str, float | int]:
    breakout_pct = ((quote.last_price - rt_state.box_high) / rt_state.box_high * 100.0) if quote and rt_state.box_high > 0 else 0.0
    volume_ratio = (quote.cum_volume_delta / rt_state.avg_box_volume) if quote and rt_state.avg_box_volume > 0 else 0.0
    trade_intensity = (quote.bid_ask_imbalance * 100.0 + 100.0) if quote else 0.0
    recent_rise_pct = 0.0
    if quote and len(quote.recent_prices) >= 3:
        first = float(quote.recent_prices[0] or 0.0)
        last = float(quote.recent_prices[-1] or 0.0)
        if first > 0:
            recent_rise_pct = (last - first) / first * 100.0
    return {
        "breakout_pct": breakout_pct,
        "volume_ratio": volume_ratio,
        "trade_intensity": trade_intensity,
        "recent_rise_pct": recent_rise_pct,
        "trade_velocity": int(quote.trade_velocity if quote else 0),
        "cum_volume_delta": int(quote.cum_volume_delta if quote else 0),
        "imbalance": float(quote.bid_ask_imbalance if quote else 0.0),
    }


def _eod_marker_path(date_str: str) -> Path:
    return Path(__file__).parents[1] / "data" / f"eod_done_{date_str}.json"


def _load_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _mark_eod_done(date_str: str, payload: dict) -> None:
    marker = _eod_marker_path(date_str)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _has_eod_done(date_str: str) -> bool:
    return _eod_marker_path(date_str).exists()


def _daily_concentration_metrics(engine: StrategyStateEngine, date_str: str) -> dict[str, float]:
    today = [t for t in engine.trades if t.entry_ts[:8] == date_str]
    total_net = sum(int(t.pnl_krw) for t in today)
    top3_sum = sum(sorted((int(t.pnl_krw) for t in today), reverse=True)[:3])
    gross_profit = sum(int(t.pnl_krw) for t in today if t.pnl_krw > 0)
    gross_loss = sum(-int(t.pnl_krw) for t in today if t.pnl_krw < 0)
    stats = engine.cumulative_stats()
    return {
        "top3_contribution": (top3_sum / total_net) if total_net > 0 else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float(gross_profit > 0),
        "cumulative_win_rate": float(stats.get("win_rate", 0.0) or 0.0) / 100.0,
    }


def _review_telegram_message(engine: StrategyStateEngine, date_str: str, review_result: dict | None, env_changed: bool, completed_at: str) -> str:
    if not review_result:
        return (
            f"🧠 *박스봇 복기 완료* {date_str}\n"
            f"  완료시각 {completed_at} KST\n"
            "  AI 복기는 비활성 상태였습니다."
        )

    concentration = _daily_concentration_metrics(engine, date_str)
    learning_points = review_result.get("learning_points") or []
    top_lines = "\n".join(f"  - {point}" for point in learning_points[:4]) if learning_points else "  - 요약 생성 없음"
    restart_line = "예" if env_changed else "아니오"
    top_exit_reason = review_result.get("top_exit_reason") or "-"
    return (
        f"🧠 *박스봇 복기 완료* {date_str}\n"
        f"  완료시각 {completed_at} KST\n"
        f"  거래 {review_result.get('trade_count', 0)}건 | 승률 {review_result.get('win_rate', 0.0):.1f}%\n"
        f"  순손익 {_signed_krw(review_result.get('net_pnl', 0))} | 기대값 {_signed_krw(review_result.get('weighted_edge', 0))}\n"
        f"  상위3 기여도 {concentration['top3_contribution']:.0%} | PF {concentration['profit_factor']:.2f} | 누적승률 {concentration['cumulative_win_rate']:.1%}\n"
        f"  주요 종료사유 {top_exit_reason} | 수정안 {review_result.get('decision_count', 0)}건 | 재시작필요 {restart_line}\n"
        f"  일지 {review_result.get('journal_path', '-')}\n"
        f"  학습핵심\n{top_lines}"
    )


def _watch_candidate_from_candles(box_checker: BoxChecker, candles: list[Candle], stk_cd: str, name: str) -> BoxRealtimeState | None:
    if len(candles) < box_checker.min_length * box_checker.aggregate_minutes:
        return None
    current = candles[-1]
    box = box_checker.preview_box(candles, stk_cd)
    if box is None:
        return None
    return BoxRealtimeState(
        code=stk_cd,
        name=name,
        box_high=box["box_high"],
        box_low=box["box_low"],
        preferred=box["preferred"],
        daily_pass=True,
        box_height_pct=box["height_pct"],
        box_length=box["length"],
        box_grade=box.get("grade", "C"),
        avg_box_volume=box.get("avg_volume", 0.0),
        status="box_building",
        last_transition_ts=current.ts,
    )


def _build_realtime_watchlist(
    stocks: list[dict],
    box_checker: BoxChecker,
    rt_client: KiwoomRealtimeClient,
    delay: float,
    excluded_codes: set[str] | None = None,
) -> dict[str, BoxRealtimeState]:
    build_started = time.time()
    excluded_codes = excluded_codes or set()
    candidates: list[tuple[dict, BoxRealtimeState]] = []
    for stk in stocks:
        stk_cd = stk.get("code", "")
        name = stk.get("name", stk_cd)
        if stk_cd in excluded_codes:
            continue
        try:
            rows = get_min_chart(stk_cd, tic_scope="1", max_pages=1)
        except Exception:
            time.sleep(delay)
            continue
        if len(rows) < 6:
            time.sleep(delay)
            continue
        candles = [Candle(**parse_candle(r)) for r in reversed(rows)]
        state = _watch_candidate_from_candles(box_checker, candles, stk_cd, name)
        if not state:
            time.sleep(delay)
            continue
        last_price = candles[-1].close
        distance_pct = ((state.box_high - last_price) / state.box_high) if state.box_high > 0 else 99.0
        candidates.append((
            {
                "code": stk_cd,
                "preferred_box": state.preferred,
                "distance_pct": distance_pct,
                "box_height_pct": state.box_height_pct,
                "box_length": state.box_length,
                "box_grade": state.box_grade,
            },
            state,
        ))
        time.sleep(delay)

    candidates.sort(key=lambda item: _watchlist_rank(item[0]), reverse=True)
    selected = candidates[:BOX_RT_WATCHLIST_MAX]
    watchlist = {state.code: state for _, state in selected}
    for code in rt_client.subscribed_codes():
        if code not in watchlist:
            rt_client.unsubscribe(code)
    for code in watchlist:
        rt_client.subscribe(code)
    logger.info(
        "실시간 watchlist 재구성: %d종목 / 후보 %d건 / 제외 %d종목 (%.1fs)",
        len(watchlist),
        len(candidates),
        len(excluded_codes),
        time.time() - build_started,
    )
    return watchlist


def _monitor_degraded_holdings_once(
    engine: StrategyStateEngine,
    broker: KisDomesticBroker | None,
    delay: float,
) -> dict[str, float]:
    """실시간 장애 시 보유 종목만 REST로 방어 감시한다."""
    latest_prices: dict[str, float] = {}
    for code in list(engine.positions.keys()):
        pos = engine.positions.get(code)
        if not pos:
            continue
        try:
            rows = get_min_chart(code, tic_scope="1", max_pages=1)
        except Exception:
            time.sleep(delay)
            continue
        if not rows:
            time.sleep(delay)
            continue
        candles = [Candle(**parse_candle(r)) for r in reversed(rows)]
        latest = candles[-1]
        latest_prices[code] = latest.close
        reason = engine.tick(code, latest.close, latest.ts, source="bar")
        if not reason:
            time.sleep(delay)
            continue
        if broker:
            qty = engine.positions.get(code).qty if engine.positions.get(code) else 0
            if qty > 0:
                order, confirmed = broker.sell_and_confirm(code, qty)
                if not order or not confirmed:
                    broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                    logger.warning("degraded 방어매도 실패/대기: %s(%s) [%s] %s", pos.name, code, reason, broker_reason)
                    time.sleep(delay)
                    continue
        exit_context = _build_exit_context(pos, exit_price=latest.close, exit_ts=latest.ts, exit_reason=reason, source="degraded_bar")
        trade = engine.sell(code, latest.close, latest.ts, reason, exit_context=exit_context)
        if trade:
            _write_trade_exit_journal(trade)
            emoji = "✅" if trade.pnl_krw >= 0 else "❌"
            logger.info(
                "%s degraded 청산 %s(%s) | 수익 %s (%.2f%%) [%s]",
                emoji,
                trade.name,
                code,
                _signed_krw(trade.pnl_krw),
                trade.pnl_pct,
                reason,
            )
            _send(
                f"{emoji} *degraded 청산* {trade.name}({code})\n"
                f"  수익 {_signed_krw(trade.pnl_krw)} ({trade.pnl_pct:+.2f}%) [{reason}]\n"
                f"  {_stats_line(engine)}"
            )
        time.sleep(delay)
    return latest_prices


def _refresh_stale_quotes_with_rest(
    rt_client: KiwoomRealtimeClient,
    watchlist: dict[str, BoxRealtimeState],
    stale_codes: set[str],
    runtime_metrics: dict,
) -> int:
    """stale 종목에 한해 REST 현재가로 마지막 상태를 보강한다."""
    refreshed = 0
    for code in sorted(stale_codes):
        if code not in watchlist:
            continue
        try:
            snapshot = get_basic_price(code)
        except Exception as exc:
            logger.debug("실시간 stale 보강 실패 %s: %s", code, exc)
            continue
        data = snapshot.get("data") if isinstance(snapshot, dict) else None
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = snapshot if isinstance(snapshot, dict) else {}
        price_raw = data.get("cur_prc") or data.get("price") or data.get("close") or data.get("curPrice") or 0
        price = float(str(price_raw).replace(",", "").lstrip("+-") or 0)
        if price <= 0:
            continue
        bid_raw = data.get("bid_pric") or data.get("best_bid") or data.get("buy_price") or price
        ask_raw = data.get("ask_pric") or data.get("best_ask") or data.get("sell_price") or price
        bid = float(str(bid_raw).replace(",", "").lstrip("+-") or 0)
        ask = float(str(ask_raw).replace(",", "").lstrip("+-") or 0)
        rt_client.inject_event(
            RealtimeTick(
                code=code,
                event_type="rest_refresh",
                price=price,
                volume=0,
                ts=datetime.now(KST).strftime("%Y%m%d%H%M%S"),
                best_bid=bid,
                best_ask=ask,
                meta={"source": "rest_refresh"},
            )
        )
        refreshed += 1
    if refreshed:
        runtime_metrics["hybrid_refresh_count"] += refreshed
        logger.info("실시간 stale 보강: %d종목 REST 현재가 주입", refreshed)
    return refreshed


def _refresh_engine_from_broker(engine: StrategyStateEngine, broker: KisDomesticBroker | None, *, reason: str) -> bool:
    if not broker:
        return False
    balance = broker.get_balance()
    holdings = broker.get_holdings()
    if not balance:
        logger.warning("KIS 동기화 실패(%s): balance empty", reason)
        return False
    cash = float(balance.get("cash", 0) or 0)
    sync_ts = datetime.now(KST).strftime("%Y%m%d%H%M%S")
    engine.sync_from_broker(cash, holdings, sync_ts)
    logger.info("KIS 재동기화 완료(%s): 현금 %s | 보유 %d종목", reason, f"₩{cash:,.0f}", len(holdings))
    return True


def _confirm_live_buy_fill(symbol: str, qty: int, broker: KisDomesticBroker | None) -> tuple[bool, int]:
    if not broker or broker.is_mock:
        return False, 0
    holdings = broker.get_holdings()
    current = next((item for item in holdings if item.get("code") == symbol), None)
    confirmed_qty = int(current.get("qty", 0) or 0) if current else 0
    return confirmed_qty >= qty, confirmed_qty


def _should_preserve_recent_local_positions(engine: StrategyStateEngine, holdings: list[dict], *, grace_sec: int = 1800) -> list[str]:
    if holdings or not engine.positions:
        return []
    now = datetime.now(KST)
    preserved: list[str] = []
    for code, pos in engine.positions.items():
        try:
            elapsed = int((now - _parse_kst_ts(pos.entry_ts)).total_seconds())
        except Exception:
            elapsed = grace_sec + 1
        if elapsed <= grace_sec:
            preserved.append(f"{pos.name}({code})/{elapsed}s")
    return preserved


def _reconcile_broker_state(
    engine: StrategyStateEngine,
    broker: KisDomesticBroker | None,
    exit_managers: dict[str, BoxLadderExit],
    *,
    reason: str,
) -> tuple[bool, list[str]]:
    if not broker:
        return False, []

    balance = broker.get_balance()
    holdings = broker.get_holdings()
    if not balance:
        logger.warning("KIS 재동기화 실패(%s): balance empty", reason)
        return False, []

    preserved_recent = _should_preserve_recent_local_positions(engine, holdings)
    if preserved_recent:
        logger.warning(
            "KIS 재동기화 보류(%s): broker 보유 0건이지만 최근 로컬 포지션 보존 %d건: %s",
            reason,
            len(preserved_recent),
            ", ".join(preserved_recent[:8]),
        )
        return False, []

    excluded_codes: list[str] = []
    excluded_labels: list[str] = []
    for item in holdings:
        blocked, blocked_reason = _is_excluded_name(item.get("name", item.get("code", "")))
        if blocked:
            code = item.get("code", "")
            excluded_codes.append(code)
            excluded_labels.append(f"{item.get('name', code)}({code})/{blocked_reason}")

    sync_ts = datetime.now(KST).strftime("%Y%m%d%H%M%S")
    engine.sync_from_broker(float(balance.get("cash", 0) or 0), holdings, sync_ts)
    for code in list(exit_managers.keys()):
        if code not in engine.positions:
            exit_managers.pop(code, None)

    if excluded_labels:
        logger.warning("전략 외 보유종목 감지 %d건(%s): %s", len(excluded_labels), reason, ", ".join(excluded_labels[:12]))

    if excluded_codes:
        for item in holdings:
            code = item.get("code", "")
            if code not in excluded_codes:
                continue
            qty = int(item.get("qty", 0) or 0)
            if qty <= 0:
                continue
            name = item.get("name", code)
            order, confirmed = broker.sell_and_confirm(code, qty)
            if order and confirmed:
                logger.info("전략 외 보유종목 자동청산 성공: %s(%s) %d주", name, code, qty)
            else:
                broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                logger.warning("전략 외 보유종목 자동청산 실패: %s(%s) %d주 | %s", name, code, qty, broker_reason)
                _send_once_per_key(
                    f"excluded_liquidation_fail:{code}",
                    f"⚠️ *박스봇 전략 외 잔고 청산 실패* {name}({code})\n"
                    f"  수량 {qty}주\n"
                    f"  KIS 사유: {broker_reason}",
                )

        balance = broker.get_balance()
        holdings = broker.get_holdings()
        if balance:
            sync_ts = datetime.now(KST).strftime("%Y%m%d%H%M%S")
            engine.sync_from_broker(float(balance.get("cash", 0) or 0), holdings, sync_ts)
            for code in list(exit_managers.keys()):
                if code not in engine.positions:
                    exit_managers.pop(code, None)

        excluded_labels = []
        for item in holdings:
            blocked, blocked_reason = _is_excluded_name(item.get("name", item.get("code", "")))
            if blocked:
                excluded_labels.append(f"{item.get('name', item.get('code', ''))}({item.get('code', '')})/{blocked_reason}")

    return True, excluded_labels


def _force_close_end_of_day(
    engine: StrategyStateEngine,
    broker: KisDomesticBroker | None,
    exit_managers: dict[str, BoxLadderExit],
    delay: float,
) -> int:
    closed_count = 0
    for code in list(engine.positions.keys()):
        pos = engine.positions.get(code)
        if not pos:
            continue

        price = 0.0
        ts = datetime.now(KST).strftime("%Y%m%d%H%M%S")
        try:
            snapshot = get_basic_price(code)
        except Exception as exc:
            logger.warning("EOD 현재가 조회 실패 %s(%s): %s", pos.name, code, exc)
            snapshot = {}

        data = snapshot.get("data") if isinstance(snapshot, dict) else None
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = snapshot if isinstance(snapshot, dict) else {}

        price_raw = data.get("cur_prc") or data.get("price") or data.get("close") or data.get("curPrice") or 0
        price = float(str(price_raw).replace(",", "").lstrip("+-") or 0)
        if price <= 0:
            price = pos.entry_price

        if broker:
            qty = engine.positions.get(code).qty if engine.positions.get(code) else 0
            if qty > 0:
                order, confirmed = broker.sell_and_confirm(code, qty)
                if not order or not confirmed:
                    broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                    logger.warning("EOD 강제청산 실패/대기: %s(%s) %d주 | %s", pos.name, code, qty, broker_reason)
                    time.sleep(delay)
                    continue

        exit_context = _build_exit_context(pos, exit_price=price, exit_ts=ts, exit_reason="eod_force", source="eod_force")
        trade = engine.sell(code, price, ts, "eod_force", exit_context=exit_context)
        if not trade:
            time.sleep(delay)
            continue

        _record_symbol_exit(trade)
        _write_trade_exit_journal(trade)
        closed_count += 1
        exit_managers.pop(code, None)
        emoji = "✅" if trade.pnl_krw >= 0 else "❌"
        logger.info(
            "%s EOD 강제청산 %s(%s) | 수익 %s (%.2f%%)",
            emoji,
            trade.name,
            code,
            _signed_krw(trade.pnl_krw),
            trade.pnl_pct,
        )
        _send(
            f"{emoji} *EOD 강제청산* {trade.name}({code})\n"
            f"  수익 {_signed_krw(trade.pnl_krw)} ({trade.pnl_pct:+.2f}%) [eod_force]\n"
            f"  {_stats_line(engine)}"
        )
        time.sleep(delay)
    return closed_count


def _load_stocks(top: int, broker: KisDomesticBroker | None = None) -> list[dict]:
    logger.info("종목 목록 조회 중... (data_source=%s)", "kis_real" if USE_KIS_REAL_DATA else "kiwoom")
    if UNIVERSE_MODE in {"kis", "kis_direct", "direct"}:
        try:
            if broker is not None:
                stocks = _kis_direct_universe(broker, top)
                _remember_universe_snapshot(stocks)
                return stocks
            if USE_KIS_REAL_DATA:
                class _KisRealUniverseAdapter:
                    def get_volume_rank(self, *, market: str = "0001") -> list[dict]:
                        return kis_get_volume_rank(market=market)
                    def get_volume_power(self, *, market: str = "0001") -> list[dict]:
                        return kis_get_volume_power(market=market)
                    def get_foreign_institution_total(self, *, market: str = "0001") -> list[dict]:
                        return kis_get_foreign_institution_total(market=market)
                stocks = _kis_direct_universe(_KisRealUniverseAdapter(), top)
                _remember_universe_snapshot(stocks)
                return stocks
        except Exception as exc:
            logger.warning("KIS 직결 유니버스 조회 실패, Kiwoom 폴백: %s", exc)
    kospi_all = get_stock_list("0")
    if UNIVERSE_MODE == "fixed":
        name_by_code = {item.get("code", ""): item.get("name", item.get("code", "")) for item in kospi_all}
        stocks = []
        excluded = []
        for code in KR_BOX_BOT_UNIVERSE_KOSPI[:top]:
            name = name_by_code.get(code, code)
            blocked, reason = _is_excluded_name(name)
            if blocked:
                excluded.append((code, name, reason))
                continue
            stocks.append({"code": code, "name": name})
        logger.info("스캔 대상: 한국 박스봇 KOSPI 고정리스트 %d종목", len(stocks))
        if excluded:
            logger.info("유니버스 제외 %d종목: %s", len(excluded), ", ".join(f"{n}({c})/{r}" for c, n, r in excluded[:12]))
        _remember_universe_snapshot(stocks)
        return stocks
    stocks = _dynamic_universe(kospi_all, top)
    _remember_universe_snapshot(stocks)
    return stocks


def _attach_holding_stocks(stocks: list[dict], engine: StrategyStateEngine) -> list[dict]:
    if not engine.positions:
        return stocks
    merged = list(stocks)
    existing = {item.get("code", "") for item in merged}
    forced: list[str] = []
    for code, pos in engine.positions.items():
        if code in existing:
            continue
        merged.insert(0, {"code": code, "name": pos.name})
        forced.append(f"{pos.name}({code})")
    if forced:
        logger.info("보유종목 스캔 강제포함 %d건: %s", len(forced), ", ".join(forced))
    return merged


def run_scan_loop(
    stocks:      list[dict],
    engine:      PaperEngine,
    box_checker: BoxChecker,
    broker:      KisDomesticBroker | None,
    exit_managers: dict[str, BoxLadderExit],
    delay:       float,
) -> dict[str, float]:
    """1분 스캔 1회 실행. 최신 가격 dict 반환."""
    h, m = _now_hm()
    allow_new_buy = (h, m) < NO_NEW_BUY
    date_str = datetime.now(KST).strftime("%Y%m%d")
    daily_buy_block_reason = _daily_buy_block_reason(date_str)
    if daily_buy_block_reason:
        allow_new_buy = False
        _send_daily_limit_alert_once(date_str, daily_buy_block_reason, engine)
    latest_prices: dict[str, float] = {}
    buy_candidates: list[dict] = []

    for stk in stocks:
        stk_cd = stk.get("code", "")
        name   = stk.get("name", stk_cd)
        if not stk_cd:
            continue
        blocked, reason = _is_excluded_name(name)
        if blocked:
            logger.info("유니버스차단 %s(%s) | 사유=%s", name, stk_cd, reason)
            continue

        try:
            rows = get_min_chart(stk_cd, tic_scope="1", max_pages=1)
        except Exception:
            time.sleep(delay)
            continue

        if len(rows) < 6:
            time.sleep(delay)
            continue

        # 최신순 → 시간순, 최근 봉
        candles = [Candle(**parse_candle(r)) for r in reversed(rows)]
        latest  = candles[-1]
        latest_prices[stk_cd] = latest.close

        # 기존 포지션 tick → 청산 판단
        if stk_cd in engine.positions:
            pos = engine.positions[stk_cd]
            if stk_cd not in exit_managers:
                exit_managers[stk_cd] = BoxLadderExit(
                    entry_price=pos.entry_price,
                    entry_box_high=pos.box_high or pos.entry_price,
                    entry_box_low=pos.box_low or pos.entry_price,
                    timeframe="1min",
                )
            reason = engine.tick(stk_cd, latest.close, latest.ts)
            if not reason:
                _, reason = exit_managers[stk_cd].update(candles)
                if reason in {"hold", "new_box_formed"}:
                    reason = None
            if reason:
                if broker:
                    qty = engine.positions[stk_cd].qty
                    order, confirmed = broker.sell_and_confirm(stk_cd, qty)
                    if not order:
                        broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                        if "잔고내역이 없습니다" in broker_reason:
                            refreshed = _refresh_engine_from_broker(engine, broker, reason=f"sell-no-holding:{stk_cd}")
                            if refreshed and stk_cd not in engine.positions:
                                logger.info("KIS 기준 이미 청산된 종목 감지: %s(%s)", name, stk_cd)
                                exit_managers.pop(stk_cd, None)
                                time.sleep(delay)
                                continue
                        logger.warning("KIS %s매도 실패로 로컬 청산 생략: %s(%s) [%s] | %s", "모의" if broker.is_mock else "실전", name, stk_cd, reason, broker_reason)
                        _send_once_per_key(
                            f"kis_mock_sell_fail:{stk_cd}:{reason}",
                            f"⚠️ *박스봇 {'모의' if broker.is_mock else '실전'}매도 실패* {name}({stk_cd})\n"
                            f"  사유 {reason}\n"
                            f"  KIS 사유: {broker_reason}\n"
                            f"  로컬 청산도 생략됨\n"
                            f"  {_stats_line(engine)}",
                        )
                        time.sleep(delay)
                        continue
                    if not confirmed:
                        _refresh_engine_from_broker(engine, broker, reason=f"sell-pending:{stk_cd}")
                        if stk_cd in engine.positions:
                            logger.warning("KIS %s매도 반영 대기: %s(%s) [%s]", "모의" if broker.is_mock else "실전", name, stk_cd, reason)
                            time.sleep(delay)
                            continue
                exit_context = _build_exit_context(pos, exit_price=latest.close, exit_ts=latest.ts, exit_reason=reason, source="scan_bar")
                trade = engine.sell(stk_cd, latest.close, latest.ts, reason, exit_context=exit_context)
                if trade:
                    _record_symbol_exit(trade)
                    _write_trade_exit_journal(trade)
                    _maybe_trigger_loss_circuit(trade, engine)
                    _set_symbol_reentry_block(stk_cd, latest.ts, reason, trade.pnl_krw)
                    exit_managers.pop(stk_cd, None)
                    emoji = "✅" if trade.pnl_krw >= 0 else "❌"
                    logger.info(
                        "%s 청산 %s(%s) | 수익 %s (%.2f%%) [%s]",
                        emoji, trade.name, stk_cd,
                        _signed_krw(trade.pnl_krw), trade.pnl_pct, reason,
                    )
                    _send(
                        f"{emoji} *청산* {trade.name}({stk_cd})\n"
                        f"  수익 {_signed_krw(trade.pnl_krw)} ({trade.pnl_pct:+.2f}%) [{reason}]\n"
                        f"  {_stats_line(engine)}"
                    )

        # 신호 검사 → 신규 매수 후보 수집
        elif allow_new_buy:
            symbol_reason = _symbol_daily_block_reason(stk_cd)
            if symbol_reason:
                logger.info("매수차단 %s(%s) | 사유=%s", name, stk_cd, symbol_reason)
                time.sleep(delay)
                continue
            box_ok, box_info = box_checker.check(candles, stk_cd)
            if not box_ok:
                trend_ok, trend_info = _check_trend_rebreak(candles)
                if trend_ok:
                    buy_candidates.append(
                        {
                            "code": stk_cd,
                            "name": name,
                            "latest": latest,
                            "box_info": trend_info,
                            "rank": _candidate_rank(trend_info),
                        }
                    )
                    logger.info(
                        "추세재돌파 후보 %s(%s) | 눌림폭=%.2f%% 재돌파=%.2f%% 몸통=%.2f",
                        name,
                        stk_cd,
                        trend_info["box_height_pct"],
                        trend_info["breakout_close_pct"],
                        trend_info["breakout_body_ratio"],
                    )
                    time.sleep(delay)
                    continue
                logger.info(
                    "BOX차단 %s(%s) | 사유=%s 높이=%.2f%% 길이=%d봉 선호=%s 저점상승=%s 일봉=%s",
                    name,
                    stk_cd,
                    box_info["reject_reason"],
                    box_info["box_height_pct"],
                    box_info["box_length"],
                    box_info["preferred_box"],
                    box_info["is_rising_lows"],
                    box_info["daily_pass"],
                )
                time.sleep(delay)
                continue

            extra_filter_reason = None
            current_hhmm = latest.ts[8:12] if len(latest.ts) >= 12 else datetime.now(KST).strftime("%H%M")
            require_preferred = REQUIRE_PREFERRED_BOX or _is_opening_window(current_hhmm)
            if require_preferred and not box_info.get("preferred_box", False):
                extra_filter_reason = "require_preferred_box"
            if extra_filter_reason:
                logger.info(
                    "매수차단 %s(%s) | 사유=%s | BOX 높이 %.2f%% 길이=%d봉 선호=%s",
                    name, stk_cd, extra_filter_reason,
                    box_info["box_height_pct"], box_info["box_length"], box_info["preferred_box"],
                )
                time.sleep(delay)
                continue

            buy_candidates.append(
                {
                    "code": stk_cd,
                    "name": name,
                    "latest": latest,
                    "box_info": box_info,
                    "rank": _candidate_rank(box_info),
                }
            )

        time.sleep(delay)

    if allow_new_buy and buy_candidates:
        buy_candidates.sort(key=lambda item: item["rank"], reverse=True)
        buy_candidates = _candidate_focus_pool(buy_candidates)
        reference_dt = _parse_kst_ts(buy_candidates[0]["latest"].ts)
        _prune_recent_entries(reference_dt)
        reference_hhmm = buy_candidates[0]["latest"].ts[8:12]
        time_policy = _time_policy(reference_hhmm)
        if time_policy == "blocked":
            logger.info("신규진입 차단 구간: %s 이전은 진입 금지", NO_BUY_BEFORE)
            return latest_prices
        scan_limit = MAX_NEW_BUYS_PER_SCAN
        ten_min_limit = MAX_NEW_BUYS_PER_10MIN
        if _is_opening_window(reference_hhmm):
            scan_limit = min(scan_limit, OPENING_MAX_BUYS_PER_SCAN)
            ten_min_limit = min(ten_min_limit, OPENING_MAX_BUYS_PER_10MIN)

        new_buys_this_scan = 0
        for candidate in buy_candidates:
            stk_cd = candidate["code"]
            name = candidate["name"]
            latest = candidate["latest"]
            box_info = candidate["box_info"]
            latest_dt = _parse_kst_ts(latest.ts)

            _prune_recent_entries(latest_dt)
            if new_buys_this_scan >= scan_limit:
                logger.info("매수차단 %s(%s) | 사유=scan_buy_limit:%d", name, stk_cd, scan_limit)
                continue
            if len(_RECENT_ENTRY_TIMES) >= ten_min_limit:
                logger.info("매수차단 %s(%s) | 사유=ten_min_buy_limit:%d", name, stk_cd, ten_min_limit)
                continue
            blocked, block_reason = _active_symbol_reentry_block(stk_cd, latest.ts)
            if blocked:
                logger.info("매수차단 %s(%s) | 사유=%s", name, stk_cd, block_reason)
                continue
            if _box_breakout_attempted(
                stk_cd,
                box_info["box_high"],
                box_info["box_low"],
                box_info["box_length"],
                box_info.get("box_grade", "C"),
                date_str,
            ):
                logger.info("매수차단 %s(%s) | 사유=box_breakout_already_attempted", name, stk_cd)
                continue

            size_multiplier, size_reason = _size_multiplier_for_entry(latest.ts)
            if size_multiplier <= 0:
                logger.info("매수차단 %s(%s) | 사유=%s", name, stk_cd, size_reason)
                continue
            can_buy, buy_reason, qty, est_cost = engine.can_buy(stk_cd, latest.close, size_multiplier=size_multiplier)
            if not can_buy:
                logger.info(
                    "매수차단 %s(%s) | 사유=%s | 후보가 %.0f원 | 예상주문 %.0f원",
                    name, stk_cd, buy_reason, latest.close, est_cost,
                )
                continue
            if broker:
                kis_qty = broker.inquire_buyable_qty(stk_cd, market_order=True, price=latest.close)
                if kis_qty <= 0:
                    broker_reason = broker.last_reject_message or broker.last_error_message or "kis_buyable_qty_zero"
                    logger.warning("KIS 매수가능수량 0으로 진입 생략: %s(%s) | %s", name, stk_cd, broker_reason)
                    _send_once_per_key(
                        f"kis_mock_buyable_zero:{stk_cd}",
                        f"⚠️ *박스봇 매수가능수량 0* {name}({stk_cd})\n"
                        f"  후보가 {latest.close:,.0f}원\n"
                        f"  KIS 사유: {broker_reason}\n"
                        f"  {_stats_line(engine)}",
                    )
                    continue
                if kis_qty < qty:
                    logger.info(
                        "KIS 수량보정 %s(%s) | 로컬 %d주 -> KIS 가능 %d주",
                        name, stk_cd, qty, kis_qty,
                    )
                    qty = kis_qty
                order, confirmed = broker.buy_and_confirm(stk_cd, qty) if _is_live_broker(broker) else (broker.buy(stk_cd, qty), True)
                if not order:
                    broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                    logger.warning("KIS %s매수 실패로 로컬 매수 생략: %s(%s) | %s", _mode_label(broker), name, stk_cd, broker_reason)
                    _send_once_per_key(
                        f"kis_mock_buy_fail:{stk_cd}",
                        f"⚠️ *박스봇 {_mode_label(broker)}매수 실패* {name}({stk_cd})\n"
                        f"  신호는 발생했지만 KIS {_mode_label(broker)}주문 실패로 진입 생략\n"
                        f"  후보가 {latest.close:,.0f}원 / 수량 {qty}주\n"
                        f"  KIS 사유: {broker_reason}\n"
                        f"  {_stats_line(engine) if not _is_live_broker(broker) else '실계좌 체결 없음'}",
                    )
                    continue
                if _is_live_broker(broker):
                    confirmed_fill, confirmed_qty = _confirm_live_buy_fill(stk_cd, qty, broker)
                    logger.info(
                        "🟢 실전 매수접수 %s(%s) | 주문가 %s | 수량 %d주 | BOX %s급 %.2f%% %d봉 | 체결확인=%s(%d주)",
                        name,
                        stk_cd,
                        f"{latest.close:,.0f}원",
                        qty,
                        box_info["box_grade"],
                        box_info["box_height_pct"],
                        box_info["box_length"],
                        "yes" if confirmed_fill else "pending",
                        confirmed_qty,
                    )
                    _send(
                        f"🟢 *실전 매수접수* {name}({stk_cd})\n"
                        f"  주문가 {latest.close:,.0f}원 / 수량 {qty}주 / 시각 {latest.ts[8:12]}\n"
                        f"  BOX {box_info['box_grade']}급 {box_info['box_height_pct']:.2f}% {box_info['box_length']}분봉\n"
                        f"  브로커 체결확인: {'확인' if confirmed_fill else f'대기({confirmed_qty}주)'}"
                    )
            entry_context = _build_entry_context(
                code=stk_cd,
                name=name,
                ts=latest.ts,
                price=latest.close,
                qty=qty,
                strategy_type=box_info.get("strategy_type", "box"),
                size_multiplier=size_multiplier,
                box_info=box_info,
                signal_summary=(
                    f"{box_info.get('strategy_type', 'box')} | BOX {box_info.get('box_grade', 'C')}급 "
                    f"{float(box_info.get('box_height_pct', 0.0) or 0.0):.2f}% {int(box_info.get('box_length', 0) or 0)}봉 "
                    f"| preferred={box_info.get('preferred_box', False)} rising_lows={box_info.get('is_rising_lows', False)} "
                    f"| daily_pass={box_info.get('daily_pass', False)}"
                ),
                extra={"candidate_rank": candidate["rank"], "focus_pool": len(buy_candidates)},
            )
            ok = engine.buy(
                stk_cd,
                name,
                latest.close,
                latest.ts,
                box_high=box_info["box_high"],
                box_low=box_info["box_low"],
                qty_override=qty,
                size_multiplier=size_multiplier,
                entry_context=entry_context,
            )
            if not ok:
                continue

            _write_trade_entry_journal(stk_cd, name, latest.ts, latest.close, qty, entry_context)
            _record_symbol_entry(stk_cd, name)
            _consume_post_circuit_recovery()
            _record_box_breakout_attempt(
                stk_cd,
                box_info["box_high"],
                box_info["box_low"],
                box_info["box_length"],
                box_info.get("box_grade", "C"),
                date_str,
            )
            new_buys_this_scan += 1
            _RECENT_ENTRY_TIMES.append(latest_dt)
            exit_managers[stk_cd] = BoxLadderExit(
                entry_price=latest.close,
                entry_box_high=box_info["box_high"],
                entry_box_low=box_info["box_low"],
                timeframe="1min",
            )
            logger.info(
                "⚡ 매수 %s(%s) | 전략=%s | 가격 %s | 사이즈 %.0f%% | BOX 높이 %.2f%% 길이 %d봉 선호=%s 저점상승=%s 일봉=%s rank=%s focus_top=%d",
                name, stk_cd, box_info.get("strategy_type", "box"), f"{latest.close:,.0f}원",
                size_multiplier * 100.0,
                box_info["box_height_pct"], box_info["box_length"],
                box_info["preferred_box"], box_info["is_rising_lows"], box_info["daily_pass"],
                candidate["rank"], len(buy_candidates),
            )
            if not _is_live_broker(broker):
                _send(
                    f"⚡ *매수* {name}({stk_cd})\n"
                    f"  전략 {box_info.get('strategy_type', 'box')}\n"
                    f"  진입가 {latest.close:,.0f}원  시각 {latest.ts[8:12]}\n"
                    f"  BOX {box_info['box_grade']}급 {box_info['box_height_pct']:.2f}% {box_info['box_length']}분봉 "
                    f"선호={box_info['preferred_box']} 저점상승={box_info['is_rising_lows']}\n"
                    f"  {_stats_line(engine)}"
                )

    return latest_prices


def _handle_realtime_exit(
    tick: RealtimeTick,
    engine: StrategyStateEngine,
    broker: KisDomesticBroker | None,
    runtime_metrics: dict,
) -> object | None:
    reason = engine.tick(tick.code, tick.price, tick.ts, source="realtime")
    if not reason:
        return None
    if reason == "follow_through_fail":
        runtime_metrics["follow_through_fail_count"] += 1
    if broker:
        pos = engine.positions.get(tick.code)
        qty = pos.qty if pos else 0
        if qty > 0:
            order, confirmed = broker.sell_and_confirm(tick.code, qty)
            if not order or not confirmed:
                broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                logger.warning("실시간 KIS 매도 실패/대기: %s(%s) [%s] %s", tick.code, tick.code, reason, broker_reason)
                return None
    exit_context = _build_exit_context(engine.positions.get(tick.code), exit_price=tick.price, exit_ts=tick.ts, exit_reason=reason, source="realtime_tick") if engine.positions.get(tick.code) else {}
    trade = engine.sell(tick.code, tick.price, tick.ts, reason, exit_context=exit_context)
    if not trade:
        return None
    emoji = "✅" if trade.pnl_krw >= 0 else "❌"
    logger.info("%s 실시간 청산 %s(%s) | 수익 %s (%.2f%%) [%s]", emoji, trade.name, tick.code, _signed_krw(trade.pnl_krw), trade.pnl_pct, reason)
    _record_symbol_exit(trade)
    _write_trade_exit_journal(trade)
    _maybe_trigger_loss_circuit(trade, engine)
    _set_symbol_reentry_block(tick.code, tick.ts, reason, trade.pnl_krw)
    _send(
        f"{emoji} *실시간 청산* {trade.name}({tick.code})\n"
        f"  수익 {_signed_krw(trade.pnl_krw)} ({trade.pnl_pct:+.2f}%) [{reason}]\n"
        f"  {_stats_line(engine)}"
    )
    return trade


def _can_open_realtime_trade(code: str, ts: str, date_str: str) -> tuple[bool, str]:
    hhmm = ts[8:12] if len(ts) >= 12 else datetime.now(KST).strftime("%H%M")
    if hhmm >= "1500":
        return False, "market_cutoff"
    daily_buy_block_reason = _daily_buy_block_reason(date_str)
    if daily_buy_block_reason:
        return False, daily_buy_block_reason
    circuit_reason = _loss_circuit_reason(ts)
    if circuit_reason:
        return False, circuit_reason
    time_policy = _time_policy(hhmm)
    if time_policy == "blocked":
        return False, f"blocked_before:{NO_BUY_BEFORE}"
    latest_dt = _parse_kst_ts(ts)
    _prune_recent_entries(latest_dt)
    scan_limit = MAX_NEW_BUYS_PER_SCAN
    ten_min_limit = MAX_NEW_BUYS_PER_10MIN
    if _is_opening_window(hhmm):
        scan_limit = min(scan_limit, OPENING_MAX_BUYS_PER_SCAN)
        ten_min_limit = min(ten_min_limit, OPENING_MAX_BUYS_PER_10MIN)
    same_minute = [t for t in _RECENT_ENTRY_TIMES if t.strftime("%Y%m%d%H%M") == latest_dt.strftime("%Y%m%d%H%M")]
    if len(same_minute) >= scan_limit:
        return False, f"signal_window_limit:{scan_limit}"
    if len(_RECENT_ENTRY_TIMES) >= ten_min_limit:
        return False, f"ten_min_buy_limit:{ten_min_limit}"
    symbol_reason = _symbol_daily_block_reason(code)
    if symbol_reason:
        return False, symbol_reason
    blocked, block_reason = _active_symbol_reentry_block(code, ts)
    if blocked:
        return False, block_reason
    return True, "ok"


def _handle_realtime_entry(
    tick: RealtimeTick,
    rt_state: BoxRealtimeState,
    engine: StrategyStateEngine,
    broker: KisDomesticBroker | None,
    state_machine: RealtimeStateMachine,
    runtime_metrics: dict,
    date_str: str,
) -> bool:
    if _box_breakout_attempted(
        tick.code,
        rt_state.box_high,
        rt_state.box_low,
        rt_state.box_length,
        rt_state.box_grade,
        date_str,
    ):
        logger.info("실시간 매수차단 %s(%s) | 사유=box_breakout_already_attempted", rt_state.name, tick.code)
        return False
    allowed, reason = _can_open_realtime_trade(tick.code, tick.ts, date_str)
    if not allowed:
        logger.info("실시간 매수차단 %s(%s) | 사유=%s", rt_state.name, tick.code, reason)
        if reason.startswith("daily_"):
            _send_daily_limit_alert_once(date_str, reason, engine)
        return False
    size_multiplier, size_reason = _size_multiplier_for_entry(tick.ts)
    if size_multiplier <= 0:
        logger.info("실시간 매수차단 %s(%s) | 사유=%s", rt_state.name, tick.code, size_reason)
        return False
    can_buy, buy_reason, qty, _ = engine.can_buy(tick.code, tick.price, size_multiplier=size_multiplier)
    if not can_buy:
        logger.info("실시간 매수차단 %s(%s) | 사유=%s", rt_state.name, tick.code, buy_reason)
        return False
    if broker:
        order, confirmed = broker.buy_and_confirm(tick.code, qty) if _is_live_broker(broker) else (broker.buy(tick.code, qty), True)
        if not order:
            broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
            logger.warning("실시간 KIS 매수 실패: %s(%s) | %s", rt_state.name, tick.code, broker_reason)
            return False
        if _is_live_broker(broker):
            confirmed_fill, confirmed_qty = _confirm_live_buy_fill(tick.code, qty, broker)
            logger.info(
                "🟢 실전 실시간 매수접수 %s(%s) | 주문가 %s | 수량 %d주 | BOX %s급 %.2f%% %d봉 | 체결확인=%s(%d주)",
                rt_state.name,
                tick.code,
                f"{tick.price:,.0f}원",
                qty,
                rt_state.box_grade,
                rt_state.box_height_pct,
                rt_state.box_length,
                "yes" if confirmed_fill else "pending",
                confirmed_qty,
            )
            _send(
                f"🟢 *실전 실시간 매수접수* {rt_state.name}({tick.code})\n"
                f"  주문가 {tick.price:,.0f}원 / 수량 {qty}주 / 시각 {tick.ts[8:12]}\n"
                f"  BOX {rt_state.box_grade}급 {rt_state.box_height_pct:.2f}% {rt_state.box_length}분봉\n"
                f"  브로커 체결확인: {'확인' if confirmed_fill else f'대기({confirmed_qty}주)'}"
            )
    entry_context = _build_entry_context(
        code=tick.code,
        name=rt_state.name,
        ts=tick.ts,
        price=tick.price,
        qty=qty,
        strategy_type="realtime_box_breakout",
        size_multiplier=size_multiplier,
        box_info={
            "box_grade": rt_state.box_grade,
            "box_height_pct": rt_state.box_height_pct,
            "box_length": rt_state.box_length,
            "box_high": rt_state.box_high,
            "box_low": rt_state.box_low,
            "preferred_box": rt_state.preferred,
            "is_rising_lows": True,
            "daily_pass": rt_state.daily_pass,
        },
        signal_summary=(
            f"realtime breakout confirm | BOX {rt_state.box_grade}급 "
            f"{rt_state.box_height_pct:.2f}% {rt_state.box_length}봉 | preferred={rt_state.preferred} "
            f"| status={rt_state.status}"
        ),
        extra={"realtime_status": rt_state.status},
    )
    ok = engine.buy(
        tick.code,
        rt_state.name,
        tick.price,
        tick.ts,
        box_high=rt_state.box_high,
        box_low=rt_state.box_low,
        size_multiplier=size_multiplier,
        entry_context=entry_context,
    )
    if not ok:
        return False
    _write_trade_entry_journal(tick.code, rt_state.name, tick.ts, tick.price, qty, entry_context)
    _record_symbol_entry(tick.code, rt_state.name)
    _consume_post_circuit_recovery()
    _record_box_breakout_attempt(
        tick.code,
        rt_state.box_high,
        rt_state.box_low,
        rt_state.box_length,
        rt_state.box_grade,
        date_str,
    )
    _RECENT_ENTRY_TIMES.append(_parse_kst_ts(tick.ts))
    state_machine.mark_holding(rt_state, tick.ts)
    logger.info(
        "⚡ 실시간 매수 %s(%s) | 가격 %s | 사이즈 %.0f%% | BOX %s급 높이 %.2f%% 길이 %d봉 선호=%s",
        rt_state.name, tick.code, f"{tick.price:,.0f}원", size_multiplier * 100.0, rt_state.box_grade, rt_state.box_height_pct, rt_state.box_length, rt_state.preferred,
    )
    if not _is_live_broker(broker):
        _send(
            f"⚡ *실시간 매수* {rt_state.name}({tick.code})\n"
            f"  진입가 {tick.price:,.0f}원 시각 {tick.ts[8:12]}\n"
            f"  BOX {rt_state.box_grade}급 {rt_state.box_height_pct:.2f}% {rt_state.box_length}분봉 선호={rt_state.preferred}\n"
            f"  {_stats_line(engine)}"
        )
    runtime_metrics["entry_filled_count"] += 1
    return True


def run_realtime_loop(
    stocks: list[dict],
    engine: StrategyStateEngine,
    box_checker: BoxChecker,
    broker: KisDomesticBroker | None,
    delay: float,
    date_str: str,
) -> None:
    rt_client = KisRealtimeClient() if os.getenv("KIS_REAL_APPKEY") else KiwoomRealtimeClient()
    rt_client.start()
    state_machine = RealtimeStateMachine()
    runtime_metrics = {
        "date": date_str,
        "status": rt_client.status,
        "stale_events": 0,
        "realtime_event_count": 0,
        "near_breakout_count": 0,
        "entry_pending_count": 0,
        "entry_filled_count": 0,
        "breakout_watch_reject_count": 0,
        "follow_through_fail_count": 0,
        "degraded_rest_checks": 0,
        "hybrid_refresh_count": 0,
        "entry_latency_samples": [],
        "entry_latency_avg_sec": 0.0,
    }
    logger.info("실시간 watchlist 후보 평가 시작: %d종목", len(stocks))
    watchlist = _build_realtime_watchlist(stocks, box_checker, rt_client, delay, _daily_blacklist_codes())
    last_refresh = time.time()
    last_runtime_flush = 0.0
    last_degraded_rest_check = 0.0
    last_hybrid_refresh = 0.0
    logger.info("실시간 러너 시작 — watchlist %d | status=%s", len(watchlist), rt_client.status)

    while _is_market_hours():
        holding_count = len(engine.positions)
        if time.time() - last_refresh >= BOX_RT_UNIVERSE_REFRESH_SEC:
            if BOX_RT_FREEZE_WATCHLIST_WHILE_HOLDING and holding_count > 0:
                logger.info("실시간 watchlist 재구성 보류: holding=%d freeze=%s", holding_count, BOX_RT_FREEZE_WATCHLIST_WHILE_HOLDING)
            else:
                watchlist = _build_realtime_watchlist(stocks, box_checker, rt_client, delay, _daily_blacklist_codes())
            last_refresh = time.time()

        for code in engine.positions:
            rt_client.subscribe(code)
            if code not in watchlist:
                pos = engine.positions[code]
                watchlist[code] = BoxRealtimeState(
                    code=code,
                    name=pos.name,
                    box_high=pos.box_high or pos.entry_price,
                    box_low=pos.box_low or pos.entry_price,
                    preferred=True,
                    daily_pass=True,
                    box_height_pct=0.0,
                    box_length=0,
                    status="holding",
                    last_transition_ts=pos.entry_ts,
                )

        stale_codes = set(rt_client.stale_codes(rt_client.subscribed_codes()))
        if stale_codes:
            runtime_metrics["stale_events"] += len(stale_codes)
            if rt_client.connected() and time.time() - last_hybrid_refresh >= BOX_RT_HYBRID_PRICE_REFRESH_SEC:
                _refresh_stale_quotes_with_rest(rt_client, watchlist, stale_codes, runtime_metrics)
                last_hybrid_refresh = time.time()

        events = rt_client.poll_events()
        if not events:
            if time.time() - last_runtime_flush >= 15:
                runtime_metrics.update(rt_client.stats_snapshot())
                _persist_realtime_metrics(runtime_metrics)
                last_runtime_flush = time.time()
            if rt_client.degraded():
                runtime_metrics["status"] = rt_client.status
                _send_once_per_key(
                    "box_rt_degraded",
                    "⚠️ *박스봇 실시간 degraded 전환*\n"
                    "  신규진입을 중단하고 보유종목만 REST 방어감시로 전환합니다.",
                )
                if time.time() - last_degraded_rest_check >= BOX_RT_DEGRADED_REST_SEC:
                    degraded_started = time.time()
                    runtime_metrics["degraded_rest_checks"] += 1
                    latest_prices = _monitor_degraded_holdings_once(engine, broker, delay)
                    logger.info(
                        "실시간 degraded 방어감시 완료 (%.1fs) | 보유 %d | 가격확인 %d",
                        time.time() - degraded_started,
                        len(engine.positions),
                        len(latest_prices),
                    )
                    last_degraded_rest_check = time.time()
                time.sleep(1)
            else:
                time.sleep(0.5)
            continue

        runtime_metrics["status"] = rt_client.status
        runtime_metrics["realtime_event_count"] += len(events)
        for tick in events:
            if tick.code in stale_codes:
                continue

            if tick.code in engine.positions:
                trade = _handle_realtime_exit(tick, engine, broker, runtime_metrics)
                if trade:
                    rt_state = watchlist.get(tick.code)
                    if rt_state:
                        state_machine.mark_cooldown(rt_state, tick.ts, "exit")
                    if BOX_RT_REBUILD_WATCHLIST_ON_EXIT and not engine.positions:
                        watchlist = _build_realtime_watchlist(stocks, box_checker, rt_client, delay, _daily_blacklist_codes())
                    continue

            rt_state = watchlist.get(tick.code)
            if not rt_state or tick.code in engine.positions:
                continue
            quote = rt_client.get_state(tick.code)
            prev_status = rt_state.status
            prev_transition_ts = rt_state.last_transition_ts
            state_machine.update(rt_state, quote, now_ts=tick.ts)
            if rt_state.status == "near_breakout" and prev_status != "near_breakout":
                runtime_metrics["near_breakout_count"] += 1
            if rt_state.status == "entry_pending" and prev_status != "entry_pending":
                runtime_metrics["entry_pending_count"] += 1
                metrics = _realtime_confirmation_snapshot(rt_state, quote)
                try:
                    base_ts = prev_transition_ts if prev_status == "near_breakout" else rt_state.last_transition_ts
                    latency_sec = int((_parse_kst_ts(tick.ts) - _parse_kst_ts(base_ts)).total_seconds())
                except Exception:
                    latency_sec = 0
                runtime_metrics["entry_latency_samples"].append(latency_sec)
                runtime_metrics["entry_latency_samples"] = runtime_metrics["entry_latency_samples"][-50:]
                runtime_metrics["entry_latency_avg_sec"] = round(
                    sum(runtime_metrics["entry_latency_samples"]) / max(len(runtime_metrics["entry_latency_samples"]), 1),
                    2,
                )
                logger.info(
                    "실시간 진입확정 후보 %s(%s) | latency=%ss trades=%s vol=%s imbalance=%.3f breakout=%.3f%% vol_ratio=%.2f intensity=%.1f recent_rise=%.3f%%",
                    rt_state.name,
                    tick.code,
                    latency_sec,
                    metrics["trade_velocity"],
                    metrics["cum_volume_delta"],
                    metrics["imbalance"],
                    metrics["breakout_pct"],
                    metrics["volume_ratio"],
                    metrics["trade_intensity"],
                    metrics["recent_rise_pct"],
                )
                _handle_realtime_entry(tick, rt_state, engine, broker, state_machine, runtime_metrics, date_str)
            elif rt_state.status == "breakout_watch" and prev_status != "breakout_watch":
                runtime_metrics["breakout_watch_reject_count"] += 1
                logger.info(
                    "실시간 breakout_watch %s(%s) | reason=%s price=%.0f",
                    rt_state.name,
                    tick.code,
                    rt_state.status_reason,
                    tick.price,
                )

        if time.time() - last_runtime_flush >= 15:
            runtime_metrics.update(rt_client.stats_snapshot())
            _persist_realtime_metrics(runtime_metrics)
            last_runtime_flush = time.time()

    runtime_metrics.update(rt_client.stats_snapshot())
    _persist_realtime_metrics(runtime_metrics)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top",    type=int,   default=200, help="KOSPI/KOSDAQ 각 상위 종목 수")
    ap.add_argument("--delay",  type=float, default=0.15)
    args = ap.parse_args()

    engine      = StrategyStateEngine()
    box_checker = BoxChecker()
    reviewer    = ProfitReviewAgent(Path(__file__).parents[1], reward_to_risk=AI_REVIEW_REWARD_TO_RISK)
    broker: KisDomesticBroker | None = None
    trading_mode = (os.getenv("KIS_TRADING_MODE", "mock").strip().lower() or "mock")
    live_confirmed = os.getenv("LIVE_TRADING_CONFIRMED", "false").lower() in {"1", "true", "yes", "on"}
    if trading_mode == "live" and not live_confirmed:
        logger.critical("🛑 실전 주문 차단: KIS_TRADING_MODE=live 이지만 LIVE_TRADING_CONFIRMED=true 가 아님")
        raise SystemExit(1)
    session_banner = f"========== LIVE SESSION START {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')} | mode={trading_mode} | state={engine.path.name} =========="
    _write_session_marker(session_banner)
    logger.info(session_banner)
    if os.getenv("KIS_MOCK_ENABLED", "true").lower() in {"1", "true", "yes", "on"} or trading_mode == "live":
        try:
            broker = KisDomesticBroker()
            balance = broker.get_balance()
            holdings = broker.get_holdings()
            if balance:
                cash = float(balance.get("cash", 0) or 0)
                total_assets = float(balance.get("total_assets", 0) or 0)
                stock_value = float(balance.get("stock_value", 0) or 0)
                weird = []
                for item in holdings:
                    blocked, reason = _is_excluded_name(item.get("name", item.get("code", "")))
                    if blocked:
                        weird.append(f"{item.get('name', item.get('code', ''))}({item.get('code', '')})/{reason}")
                sync_ts = datetime.now(KST).strftime("%Y%m%d%H%M%S")
                engine.sync_from_broker(cash, holdings, sync_ts)
                logger.info(
                    "KIS %s계좌 동기화 완료: 현금 %s | 보유 %d종목 | 주식평가 %s | 총자산 %s",
                    "모의" if broker.is_mock else "실전",
                    f"₩{cash:,.0f}",
                    len(holdings),
                    f"₩{stock_value:,.0f}",
                    f"₩{total_assets:,.0f}",
                )
                if weird:
                    logger.warning("전략 외 보유종목 감지 %d건: %s", len(weird), ", ".join(weird[:12]))
                    if KIS_AUTO_LIQUIDATE_EXCLUDED:
                        _, remaining_excluded = _reconcile_broker_state(
                            engine,
                            broker,
                            {},
                            reason="startup-liquidation",
                        )
                        if remaining_excluded:
                            logger.warning(
                                "시작 직후 전략 외 잔고 잔존 %d건: %s",
                                len(remaining_excluded),
                                ", ".join(remaining_excluded[:12]),
                            )
                logger.info("KIS %s주문 연동 활성화", "모의" if broker.is_mock else "실전")
            else:
                logger.warning("KIS %s주문 대기 상태: 시작 시 계좌조회 실패, 봇은 계속 실행", trading_mode)
        except Exception as exc:
            logger.warning("KIS %s주문 비활성화: %s", trading_mode, exc)
    exit_managers: dict[str, BoxLadderExit] = {}
    logger.info("박스봇 로드 완료 (전략=BOX ONLY, block_windows=%s)", "disabled")

    date_str = datetime.now(KST).strftime("%Y%m%d")
    _rebuild_symbol_daily_state(engine, date_str)
    _BOX_BREAKOUT_ATTEMPTS.clear()
    stocks   = _attach_holding_stocks(_load_stocks(args.top, broker), engine)

    report_done       = _has_eod_done(date_str)
    eod_liquidated    = False
    last_stock_refresh = time.time()
    last_heartbeat_ts = 0.0
    last_kis_sync_ts = 0.0
    last_excluded_liquidation_ts = 0.0
    kis_sync_fail_streak = 0

    logger.info("박스봇 데일리 러너 시작 — %s", date_str)
    _send(
        f"🚀 박스봇 시작 — {date_str}\n"
        f"  대상 KOSPI {len(stocks)}종목 / 전략 BOX ONLY / 시간제한 OFF\n"
        f"  하트비트 {'ON' if HEARTBEAT_ENABLED else 'OFF'} {HEARTBEAT_INTERVAL_MIN}분"
    )
    if HEARTBEAT_ENABLED:
        _send(_heartbeat_message(engine))
        last_heartbeat_ts = time.time()

    while True:
        h, m = _now_hm()
        now_dt = datetime.now(KST)
        today  = now_dt.strftime("%Y%m%d")

        # ── 날짜가 바뀌면 플래그 초기화 ──────────────────────────────────────
        if today != date_str:
            date_str          = today
            _rebuild_symbol_daily_state(engine, date_str)
            _BOX_BREAKOUT_ATTEMPTS.clear()
            report_done       = _has_eod_done(date_str)
            eod_liquidated    = False
            last_stock_refresh = 0   # 종목 목록 즉시 갱신
            logger.info("새 거래일 시작 — %s", date_str)
            _send(
                f"🚀 박스봇 시작 — {date_str}\n"
                f"  대상 KOSPI {len(stocks)}종목 / 전략 BOX ONLY / 시간제한 OFF"
            )
            if HEARTBEAT_ENABLED:
                last_heartbeat_ts = time.time()

        if (h, m) >= FORCE_FLAT and not eod_liquidated and not report_done:
            closed_count = _force_close_end_of_day(engine, broker, exit_managers, args.delay)
            eod_liquidated = True
            logger.info("EOD 강제청산 완료: %d건", closed_count)
            if broker:
                _refresh_engine_from_broker(engine, broker, reason="eod-force-close")

        # ── EOD 리포트 ───────────────────────────────────────────────────────
        if (h, m) >= SEND_REPORT and not report_done:
            from realtime.eod_reporter import send_eod_report
            env_before = _load_text(reviewer.override_env_path)
            report_done = True
            _mark_eod_done(
                date_str,
                {
                    "date": date_str,
                    "completed_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                    "timezone": "Asia/Seoul",
                    "status": "started",
                },
            )
            send_eod_report(engine, date_str, mode=trading_mode)
            review_result = None
            completed_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            if AI_REVIEW_ENABLED:
                review_result = reviewer.run(date_str, apply=True)
                logger.info(
                    "AI 복기 완료: trades=%d decisions=%d journal=%s",
                    review_result["trade_count"],
                    review_result["decision_count"],
                    review_result["journal_path"],
                )
            env_after = _load_text(reviewer.override_env_path)
            _mark_eod_done(
                date_str,
                {
                    "date": date_str,
                    "completed_at": completed_at,
                    "timezone": "Asia/Seoul",
                    "ai_review_enabled": AI_REVIEW_ENABLED,
                    "journal_path": review_result["journal_path"] if review_result else None,
                    "decision_count": review_result["decision_count"] if review_result else 0,
                    "env_changed": env_before != env_after,
                    "trade_count": review_result["trade_count"] if review_result else 0,
                    "net_pnl": review_result["net_pnl"] if review_result else 0,
                    "weighted_edge": review_result["weighted_edge"] if review_result else 0,
                    "learning_points": review_result["learning_points"] if review_result else [],
                },
            )
            _send(_review_telegram_message(engine, date_str, review_result, env_before != env_after, completed_at))
            logger.info("EOD 리포트 전송 완료")
            if AI_REVIEW_ENABLED and env_before != env_after:
                logger.info("AI 수정안 적용 위해 서비스 재기동 유도")
                _send_once_per_key(
                    f"ai_review_restart:{date_str}",
                    "🧠 *박스봇 AI 복기 반영 완료*\n"
                    "  저녁 복기 수정안이 저장되어 서비스를 재기동해 다음 세션 설정에 반영합니다.",
                    cooldown_sec=86400,
                )
                raise SystemExit(17)

        # ── 장 중 스캔 ────────────────────────────────────────────────────────
        if _is_market_hours():
            if BOX_RT_ENABLED:
                try:
                    run_realtime_loop(stocks, engine, box_checker, broker, args.delay, date_str)
                except Exception as exc:
                    logger.exception("실시간 루프 오류, 폴백 스캔 전환: %s", exc)
                    _send_once_per_key(
                        "realtime_loop_fail",
                        "⚠️ *박스봇 실시간 루프 실패*\n"
                        f"  {exc}\n"
                        "  기존 분봉 스캔 모드로 폴백합니다.",
                        cooldown_sec=1800,
                    )
            if HEARTBEAT_ENABLED and time.time() - last_heartbeat_ts >= HEARTBEAT_INTERVAL_MIN * 60:
                _send(_heartbeat_message(engine))
                last_heartbeat_ts = time.time()
            if broker and time.time() - last_kis_sync_ts >= KIS_SYNC_INTERVAL_SEC:
                ok, excluded_labels = _reconcile_broker_state(
                    engine,
                    broker,
                    exit_managers,
                    reason="periodic",
                )
                last_kis_sync_ts = time.time()
                if ok:
                    kis_sync_fail_streak = 0
                    last_excluded_liquidation_ts = time.time()
                    if excluded_labels:
                        _send_once_per_key(
                            "excluded_positions_present",
                            "⚠️ *박스봇 전략 외 잔고 감지*\n"
                            f"  {', '.join(excluded_labels[:6])}\n"
                            f"  {_stats_line(engine)}",
                            cooldown_sec=max(KIS_EXCLUDED_RETRY_INTERVAL_SEC, ALERT_COOLDOWN_SEC),
                        )
                else:
                    kis_sync_fail_streak += 1
                    logger.warning("KIS 재동기화 연속 실패: %d회", kis_sync_fail_streak)
                    if kis_sync_fail_streak >= 3:
                        _send_once_per_key(
                            "kis_sync_streak",
                            "❌ *박스봇 KIS 재동기화 연속 실패*\n"
                            f"  실패횟수 {kis_sync_fail_streak}회\n"
                            "  로컬 상태로는 계속 동작하지만 즉시 점검이 필요합니다.",
                            cooldown_sec=1800,
                        )
            # 동적 유니버스는 장중 지속 갱신
            if time.time() - last_stock_refresh > UNIVERSE_REFRESH_SEC:
                stocks = _attach_holding_stocks(_load_stocks(args.top, broker), engine)
                last_stock_refresh = time.time()
                logger.info("동적 유니버스 갱신 완료: %d종목 (주기 %ds)", len(stocks), UNIVERSE_REFRESH_SEC)

            scan_start = time.time()
            run_scan_loop(stocks, engine, box_checker, broker, exit_managers, args.delay)
            elapsed = time.time() - scan_start

            sleep_sec = max(5, 60 - elapsed)
            logger.info(
                "스캔 완료 (%.1f초) → %.0f초 대기 | 포지션 %d개 | 잔고 %s원",
                elapsed, sleep_sec, len(engine.positions), f"{engine.cash:,.0f}",
            )
            time.sleep(sleep_sec)
        else:
            time.sleep(120)


if __name__ == "__main__":
    main()
