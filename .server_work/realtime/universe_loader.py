from __future__ import annotations

import logging
import os
from collections import defaultdict

from collector.kiwoom_client import get_stock_list
from collector.surge_detector import Candle

logger = logging.getLogger(__name__)

UNIVERSE_MODE = os.getenv("BOX_BOT_UNIVERSE_MODE", "dynamic").strip().lower() or "dynamic"
UNIVERSE_TOP_N = int(os.getenv("BOX_BOT_UNIVERSE_TOP_N", "200") or 200)
UNIVERSE_MIN_CHANGE_PCT = float(os.getenv("BOX_BOT_UNIVERSE_MIN_CHANGE_PCT", "0.0") or 0.0)
UNIVERSE_SECTOR_BOOST_ENABLED = os.getenv("BOX_BOT_UNIVERSE_SECTOR_BOOST_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
UNIVERSE_BROKER_MARKET = os.getenv("BOX_BOT_UNIVERSE_BROKER_MARKET", "0001").strip() or "0001"
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
UNIVERSE_EMPTY_FALLBACK_ENABLED = os.getenv("BOX_BOT_UNIVERSE_EMPTY_FALLBACK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

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


def to_float(value, default: float = 0.0) -> float:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text:
            return default
        return float(text.lstrip("+"))
    except Exception:
        return default


def to_int(value, default: int = 0) -> int:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text:
            return default
        return int(float(text.lstrip("+")))
    except Exception:
        return default


def is_excluded_name(name: str) -> tuple[bool, str]:
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


def stock_row_metrics(row: dict) -> dict:
    close = to_float(row.get("cur_prc") or row.get("close") or row.get("price") or row.get("last") or row.get("lastPrice"))
    volume = to_int(row.get("trde_qty") or row.get("volume") or row.get("acc_trde_qty"))
    listed_shares = to_int(row.get("listCount"))
    turnover = to_float(row.get("trde_amt") or row.get("acc_trde_amt") or row.get("deal_amt") or row.get("trading_value"))
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    market_cap_proxy = close * listed_shares if close > 0 and listed_shares > 0 else 0.0
    if turnover <= 0 and market_cap_proxy > 0:
        turnover = market_cap_proxy
    change_pct = to_float(row.get("flu_rt") or row.get("rate") or row.get("chg_rt") or row.get("change_rate"))
    return {
        "close": close,
        "volume": volume,
        "listed_shares": listed_shares,
        "turnover": turnover,
        "market_cap_proxy": market_cap_proxy,
        "change_pct": change_pct,
    }


def classify_sector(name: str, sector_hint: str = "") -> str:
    normalized = f"{(sector_hint or '').strip()} {(name or '').strip()}".lower()
    if not normalized:
        return "other"
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(keyword.lower() in normalized for keyword in keywords):
            return sector
    return "other"


def _finalize_dynamic_selection(
    ranked: list[dict],
    excluded: list[tuple[str, str, str]],
    *,
    top: int,
    min_change_pct: float,
    original_rows: list[dict] | None = None,
    fallback_used: bool = False,
) -> list[dict]:
    selected = ranked[:max(1, min(top, UNIVERSE_TOP_N))]
    logger.info(
        "스캔 대상: 한국 박스봇 동적 유니버스 %d종목 (mode=%s, min_change=%.2f%%, sector_boost=%s%s)",
        len(selected),
        UNIVERSE_MODE,
        min_change_pct,
        "on" if UNIVERSE_SECTOR_BOOST_ENABLED else "off",
        ", fallback" if fallback_used else "",
    )
    if not selected and min_change_pct > 0 and UNIVERSE_EMPTY_FALLBACK_ENABLED:
        logger.warning(
            "동적 유니버스 0종목: min_change=%.2f%% 조건이 과도해 0.00%%로 완화 재시도",
            min_change_pct,
        )
        return dynamic_universe(
            kospi_all=original_rows or [],
            top=top,
            min_change_pct=0.0,
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


def dynamic_universe(
    kospi_all: list[dict],
    top: int,
    *,
    min_change_pct: float | None = None,
    precomputed_rows: list[dict] | None = None,
) -> list[dict]:
    min_change = UNIVERSE_MIN_CHANGE_PCT if min_change_pct is None else min_change_pct
    ranked: list[dict] = []
    excluded: list[tuple[str, str, str]] = []
    source_rows = precomputed_rows if precomputed_rows is not None else kospi_all
    for row in source_rows:
        code = row.get("code", "")
        name = row.get("name", code)
        if not code:
            continue
        if precomputed_rows is not None:
            if row.get("change_pct", 0.0) < min_change:
                continue
            ranked.append(dict(row))
            continue
        blocked, reason = is_excluded_name(name)
        if blocked:
            excluded.append((code, name, reason))
            continue
        metrics = stock_row_metrics(row)
        if metrics["close"] <= 0 or metrics["turnover"] <= 0:
            continue
        if metrics["change_pct"] < min_change:
            continue
        sector_hint = row.get("upName", "")
        sector = classify_sector(name, sector_hint)
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
            sector_scores[sector] = positive_count * 0.8 + avg_change * 1.2 + min(total_turnover / 1_000_000_000_000, 6.0)
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
    return _finalize_dynamic_selection(
        ranked,
        excluded,
        top=top,
        min_change_pct=min_change,
        original_rows=source_rows if precomputed_rows is None else None,
        fallback_used=min_change_pct is not None,
    )


def check_trend_rebreak(candles: list[Candle]) -> tuple[bool, dict]:
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
        "breakout_volume_strength": 0.0,
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
    info["breakout_volume_strength"] = round(vol_ratio, 3)
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


def broker_direct_universe(broker, top: int) -> list[dict]:
    volume_rows = broker.get_volume_rank(market=UNIVERSE_BROKER_MARKET)
    power_rows = broker.get_volume_power(market=UNIVERSE_BROKER_MARKET)
    foreign_rows = broker.get_foreign_institution_total(market=UNIVERSE_BROKER_MARKET)
    merged: dict[str, dict] = {}

    def ensure_item(code: str, name: str) -> dict:
        item = merged.get(code)
        if item is None:
            item = {
                "code": code, "name": name or code, "close": 0.0, "volume": 0, "turnover": 0.0,
                "listed_shares": 0, "market_cap_proxy": 0.0, "change_pct": 0.0,
                "volume_rank_score": 0.0, "volume_power_score": 0.0, "foreign_score": 0.0,
                "sector": "other", "sector_hint": "",
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
        blocked, _ = is_excluded_name(name)
        if blocked:
            continue
        item = ensure_item(code, name)
        item["close"] = max(item["close"], to_float(row.get("stck_prpr")))
        item["volume"] = max(item["volume"], to_int(row.get("acml_vol")))
        item["turnover"] = max(item["turnover"], to_float(row.get("acml_tr_pbmn")))
        item["listed_shares"] = max(item["listed_shares"], to_int(row.get("lstn_stcn")))
        if item["close"] > 0 and item["listed_shares"] > 0:
            item["market_cap_proxy"] = max(item["market_cap_proxy"], item["close"] * item["listed_shares"])
        item["change_pct"] = to_float(row.get("prdy_ctrt"), item["change_pct"])
        item["volume_rank_score"] = max(item["volume_rank_score"], float(len(volume_rows) - idx))

    for idx, row in enumerate(power_rows):
        code = str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or "").strip()
        if not code:
            continue
        name = str(row.get("hts_kor_isnm") or code).strip()
        blocked, _ = is_excluded_name(name)
        if blocked:
            continue
        item = ensure_item(code, name)
        item["close"] = max(item["close"], to_float(row.get("stck_prpr")))
        item["volume"] = max(item["volume"], to_int(row.get("acml_vol")))
        if item["close"] > 0 and item["volume"] > 0:
            item["turnover"] = max(item["turnover"], item["close"] * item["volume"])
        item["change_pct"] = to_float(row.get("prdy_ctrt"), item["change_pct"])
        item["volume_power_score"] = max(item["volume_power_score"], to_float(row.get("tday_rltv")))

    for idx, row in enumerate(foreign_rows):
        code = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "").strip()
        if not code:
            continue
        name = str(row.get("hts_kor_isnm") or code).strip()
        blocked, _ = is_excluded_name(name)
        if blocked:
            continue
        item = ensure_item(code, name)
        item["close"] = max(item["close"], to_float(row.get("stck_prpr")))
        item["volume"] = max(item["volume"], to_int(row.get("acml_vol")))
        if item["close"] > 0 and item["volume"] > 0:
            item["turnover"] = max(item["turnover"], item["close"] * item["volume"])
        item["change_pct"] = to_float(row.get("prdy_ctrt"), item["change_pct"])
        item["foreign_score"] = max(item["foreign_score"], to_float(row.get("frgn_ntby_tr_pbmn")) + to_float(row.get("orgn_ntby_tr_pbmn")) * 0.5)
        item["foreign_rank_score"] = max(item.get("foreign_rank_score", 0.0), float(len(foreign_rows) - idx))

    ranked: list[dict] = []
    for item in merged.values():
        if item["close"] <= 0:
            continue
        if item["change_pct"] < UNIVERSE_MIN_CHANGE_PCT:
            continue
        item["sector"] = classify_sector(item["name"], "")
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
        key=lambda item: (item.get("power_quality_hits", 0), item.get("volume_power_score", 0.0), item.get("change_pct", 0.0), item.get("turnover", 0.0)),
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
    selected.sort(key=lambda item: (item.get("leader_score", 0.0), item.get("volume_power_score", 0.0), item.get("turnover", 0.0)), reverse=True)
    logger.info(
        "스캔 대상: 한국 박스봇 브로커 직결 유니버스 %d종목 (거래량=%d, 체결강도=%d, 외국인/기관=%d, power_slots=%d/%d)",
        len(selected), len(volume_rows), len(power_rows), len(foreign_rows), power_added, UNIVERSE_POWER_SLOTS,
    )
    if not selected and UNIVERSE_MIN_CHANGE_PCT > 0 and UNIVERSE_EMPTY_FALLBACK_ENABLED:
        logger.warning(
            "브로커 직결 유니버스 0종목: min_change=%.2f%% 조건이 과도해 0.00%%로 완화 재시도",
            UNIVERSE_MIN_CHANGE_PCT,
        )
        relaxed_rows = []
        for item in merged.values():
            if item["close"] <= 0:
                continue
            clone = dict(item)
            clone["sector"] = classify_sector(clone["name"], "")
            relaxed_rows.append(clone)
        relaxed_rows.sort(
            key=lambda item: (
                item.get("leader_score", 0.0),
                item.get("volume_power_score", 0.0),
                item.get("turnover", 0.0),
            ),
            reverse=True,
        )
        selected = relaxed_rows[:max(1, min(top, UNIVERSE_TOP_N))]
        logger.info("브로커 직결 유니버스 fallback 결과: %d종목", len(selected))
    if selected:
        preview = ", ".join(
            f"{item['name']}({item['code']})/점수{item.get('leader_score', 0.0):.1f}/체결강도{item.get('volume_power_score', 0.0):.1f}/등락{item['change_pct']:+.2f}%/품질{item.get('power_quality_hits', 0)}"
            for item in selected[:12]
        )
        logger.info("브로커 유니버스 상위: %s", preview)
    return selected


def load_stocks(top: int, broker=None) -> list[dict]:
    logger.info("종목 목록 조회 중...")
    if UNIVERSE_MODE in {"broker", "kis", "kis_direct", "direct"} and broker is not None:
        try:
            return broker_direct_universe(broker, top)
        except Exception as exc:
            logger.warning("브로커 직결 유니버스 조회 실패, Kiwoom 폴백: %s", exc)
    kospi_all = get_stock_list("0")
    if UNIVERSE_MODE == "fixed":
        name_by_code = {item.get("code", ""): item.get("name", item.get("code", "")) for item in kospi_all}
        stocks = []
        excluded = []
        for code in KR_BOX_BOT_UNIVERSE_KOSPI[:top]:
            name = name_by_code.get(code, code)
            blocked, reason = is_excluded_name(name)
            if blocked:
                excluded.append((code, name, reason))
                continue
            stocks.append({"code": code, "name": name})
        logger.info("스캔 대상: 한국 박스봇 KOSPI 고정리스트 %d종목", len(stocks))
        if excluded:
            logger.info("유니버스 제외 %d종목: %s", len(excluded), ", ".join(f"{n}({c})/{r}" for c, n, r in excluded[:12]))
        return stocks
    return dynamic_universe(kospi_all, top)


def attach_holding_stocks(stocks: list[dict], positions: dict[str, object]) -> list[dict]:
    if not positions:
        return stocks
    merged = list(stocks)
    existing = {item.get("code", "") for item in merged}
    forced: list[str] = []
    for code, pos in positions.items():
        if code in existing:
            continue
        name = getattr(pos, "name", code)
        merged.insert(0, {"code": code, "name": name})
        forced.append(f"{name}({code})")
    if forced:
        logger.info("보유종목 스캔 강제포함 %d건: %s", len(forced), ", ".join(forced))
    return merged
