"""
한국 박스봇 실운용 데일리 러너

매 1분마다 KOSPI 상위 종목만 스캔해 박스 돌파 신호만으로 진입한다.
보유 포지션은 박스 청산 규칙과 손절 규칙으로 관리한다.
신규 매수는 15:00까지만 허용하고, 보유는 장 종료 후에도 유지하는 스윙형으로 운용한다.
15:30 EOD 텔레그램 리포트.

실행: python -m realtime.daily_runner
     python -m realtime.daily_runner --top 200 --delay 0.15

현재 한국봇/박스봇이 말하는 기본 전략은 BoxChecker v1 이다.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env")
load_dotenv(Path(__file__).parents[1] / ".env.ai_overrides", override=True)

from collector.kiwoom_client import get_stock_list, get_min_chart, parse_candle
from collector.surge_detector import Candle
from ai_reviewer import ProfitReviewAgent
from realtime.paper_engine import PaperEngine
from realtime.box_checker import BoxChecker
from realtime.box_ladder_exit import BoxLadderExit
from realtime.kis_mock_broker import KisMockDomesticBroker

KST = ZoneInfo("Asia/Seoul")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MARKET_OPEN  = (9, 0)
NO_NEW_BUY   = (15, 0)    # 15:00 이후 신규 진입 중단
SEND_REPORT  = (15, 30)   # EOD 리포트 발송
AI_REVIEW_ENABLED = os.getenv("BOX_BOT_AI_REVIEW_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
AI_REVIEW_REWARD_TO_RISK = float(os.getenv("BOX_BOT_REWARD_TO_RISK_TARGET", "2.0") or 2.0)
REQUIRE_PREFERRED_BOX = os.getenv("BOX_BOT_REQUIRE_PREFERRED_BOX", "false").lower() in {"1", "true", "yes", "on"}
MAX_NEW_BUYS_PER_SCAN = int(os.getenv("BOX_BOT_MAX_NEW_BUYS_PER_SCAN", "99") or 99)
MAX_NEW_BUYS_PER_10MIN = int(os.getenv("BOX_BOT_MAX_NEW_BUYS_PER_10MIN", "99") or 99)
HEARTBEAT_ENABLED = os.getenv("PATTERN_HEARTBEAT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
HEARTBEAT_INTERVAL_MIN = int(os.getenv("PATTERN_HEARTBEAT_INTERVAL_MIN", "60") or 60)
ALERT_COOLDOWN_SEC = int(os.getenv("PATTERN_ALERT_COOLDOWN_SEC", "900") or 900)
KIS_SYNC_INTERVAL_SEC = int(os.getenv("KIS_SYNC_INTERVAL_SEC", "600") or 600)
KIS_AUTO_LIQUIDATE_EXCLUDED = os.getenv("KIS_AUTO_LIQUIDATE_EXCLUDED", "true").lower() in {"1", "true", "yes", "on"}
KIS_EXCLUDED_RETRY_INTERVAL_SEC = int(os.getenv("KIS_EXCLUDED_RETRY_INTERVAL_SEC", "900") or 900)
_LAST_ALERT_TS: dict[str, float] = defaultdict(float)
_RECENT_ENTRY_TIMES: list[datetime] = []

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


def _signed_krw(value: float | int) -> str:
    value_int = int(round(float(value)))
    return f"{value_int:+,}원"


def _stats_line(engine: PaperEngine) -> str:
    stats = engine.cumulative_stats()
    return (
        f"누적손익 {_signed_krw(stats['net_pnl'])} | "
        f"누적이익 {_signed_krw(stats['realized_profit'])} | "
        f"누적손실 -{int(stats['realized_loss']):,}원 | "
        f"체결 {stats['trade_count']} | 승률 {stats['win_rate']}% | "
        f"보유 {stats['positions']} | 현금 {int(stats['cash']):,}원"
    )


def _heartbeat_message(engine: PaperEngine) -> str:
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


def _refresh_engine_from_broker(engine: PaperEngine, broker: KisMockDomesticBroker | None, *, reason: str) -> bool:
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


def _reconcile_broker_state(
    engine: PaperEngine,
    broker: KisMockDomesticBroker | None,
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


def _load_stocks(top: int) -> list[dict]:
    logger.info("종목 목록 조회 중...")
    kospi_all = get_stock_list("0")
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
    return stocks


def run_scan_loop(
    stocks:      list[dict],
    engine:      PaperEngine,
    box_checker: BoxChecker,
    broker:      KisMockDomesticBroker | None,
    exit_managers: dict[str, BoxLadderExit],
    delay:       float,
) -> dict[str, float]:
    """1분 스캔 1회 실행. 최신 가격 dict 반환."""
    h, m = _now_hm()
    allow_new_buy = (h, m) < NO_NEW_BUY
    latest_prices: dict[str, float] = {}
    new_buys_this_scan = 0

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
                        logger.warning("KIS 모의매도 실패로 로컬 청산 생략: %s(%s) [%s] | %s", name, stk_cd, reason, broker_reason)
                        _send_once_per_key(
                            f"kis_mock_sell_fail:{stk_cd}:{reason}",
                            f"⚠️ *박스봇 모의매도 실패* {name}({stk_cd})\n"
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
                            logger.warning("KIS 모의매도 반영 대기: %s(%s) [%s]", name, stk_cd, reason)
                            time.sleep(delay)
                            continue
                trade = engine.sell(stk_cd, latest.close, latest.ts, reason)
                if trade:
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

        # 신호 검사 → 신규 매수
        elif allow_new_buy:
            box_ok, box_info = box_checker.check(candles, stk_cd)
            if not box_ok:
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
            if REQUIRE_PREFERRED_BOX and not box_info.get("preferred_box", False):
                extra_filter_reason = "require_preferred_box"
            if extra_filter_reason:
                logger.info(
                    "매수차단 %s(%s) | 사유=%s | BOX 높이 %.2f%% 길이=%d봉 선호=%s",
                    name, stk_cd, extra_filter_reason,
                    box_info["box_height_pct"], box_info["box_length"], box_info["preferred_box"],
                )
                time.sleep(delay)
                continue

            latest_dt = _parse_kst_ts(latest.ts)
            _prune_recent_entries(latest_dt)
            if new_buys_this_scan >= MAX_NEW_BUYS_PER_SCAN:
                logger.info("매수차단 %s(%s) | 사유=scan_buy_limit:%d", name, stk_cd, MAX_NEW_BUYS_PER_SCAN)
                time.sleep(delay)
                continue
            if len(_RECENT_ENTRY_TIMES) >= MAX_NEW_BUYS_PER_10MIN:
                logger.info("매수차단 %s(%s) | 사유=ten_min_buy_limit:%d", name, stk_cd, MAX_NEW_BUYS_PER_10MIN)
                time.sleep(delay)
                continue

            can_buy, buy_reason, qty, est_cost = engine.can_buy(stk_cd, latest.close)
            if not can_buy:
                logger.info(
                    "매수차단 %s(%s) | 사유=%s | 후보가 %.0f원 | 예상주문 %.0f원",
                    name, stk_cd, buy_reason, latest.close, est_cost,
                )
                time.sleep(delay)
                continue
            if broker:
                order = broker.buy(stk_cd, qty)
                if not order:
                    broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                    logger.warning("KIS 모의매수 실패로 로컬 매수 생략: %s(%s) | %s", name, stk_cd, broker_reason)
                    _send_once_per_key(
                        f"kis_mock_buy_fail:{stk_cd}",
                        f"⚠️ *박스봇 모의매수 실패* {name}({stk_cd})\n"
                        f"  신호는 발생했지만 KIS 모의주문 실패로 진입 생략\n"
                        f"  후보가 {latest.close:,.0f}원 / 수량 {qty}주\n"
                        f"  KIS 사유: {broker_reason}\n"
                        f"  {_stats_line(engine)}",
                    )
                    time.sleep(delay)
                    continue
            ok = engine.buy(
                stk_cd,
                name,
                latest.close,
                latest.ts,
                box_high=box_info["box_high"],
                box_low=box_info["box_low"],
            )
            if ok:
                new_buys_this_scan += 1
                _RECENT_ENTRY_TIMES.append(latest_dt)
                exit_managers[stk_cd] = BoxLadderExit(
                    entry_price=latest.close,
                    entry_box_high=box_info["box_high"],
                    entry_box_low=box_info["box_low"],
                    timeframe="1min",
                )
                logger.info(
                    "⚡ 매수 %s(%s) | 가격 %s | BOX 높이 %.2f%% 길이 %d봉 선호=%s 저점상승=%s 일봉=%s",
                    name, stk_cd, f"{latest.close:,.0f}원",
                    box_info["box_height_pct"], box_info["box_length"],
                    box_info["preferred_box"], box_info["is_rising_lows"], box_info["daily_pass"],
                )
                _send(
                    f"⚡ *매수* {name}({stk_cd})\n"
                    f"  진입가 {latest.close:,.0f}원  시각 {latest.ts[8:12]}\n"
                    f"  BOX {box_info['box_height_pct']:.2f}% {box_info['box_length']}분봉 "
                    f"선호={box_info['preferred_box']} 저점상승={box_info['is_rising_lows']}\n"
                    f"  {_stats_line(engine)}"
                )

        time.sleep(delay)

    return latest_prices


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top",    type=int,   default=200, help="KOSPI/KOSDAQ 각 상위 종목 수")
    ap.add_argument("--delay",  type=float, default=0.15)
    args = ap.parse_args()

    engine      = PaperEngine()
    box_checker = BoxChecker()
    reviewer    = ProfitReviewAgent(Path(__file__).parents[1], reward_to_risk=AI_REVIEW_REWARD_TO_RISK)
    broker: KisMockDomesticBroker | None = None
    if os.getenv("KIS_MOCK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        try:
            broker = KisMockDomesticBroker()
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
                    "KIS 모의계좌 동기화 완료: 현금 %s | 보유 %d종목 | 주식평가 %s | 총자산 %s",
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
            logger.info("KIS 모의주문 연동 활성화")
        except Exception as exc:
            logger.warning("KIS 모의주문 비활성화: %s", exc)
    exit_managers: dict[str, BoxLadderExit] = {}
    logger.info("박스봇 로드 완료 (전략=BOX ONLY, block_windows=%s)", "disabled")

    date_str = datetime.now(KST).strftime("%Y%m%d")
    stocks   = _load_stocks(args.top)

    report_done       = False
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
            report_done       = False
            last_stock_refresh = 0   # 종목 목록 즉시 갱신
            logger.info("새 거래일 시작 — %s", date_str)
            _send(
                f"🚀 박스봇 시작 — {date_str}\n"
                f"  대상 KOSPI {len(stocks)}종목 / 전략 BOX ONLY / 시간제한 OFF"
            )
            if HEARTBEAT_ENABLED:
                last_heartbeat_ts = time.time()

        # ── EOD 리포트 ───────────────────────────────────────────────────────
        if (h, m) >= SEND_REPORT and not report_done:
            from realtime.eod_reporter import send_eod_report
            send_eod_report(engine, date_str)
            if AI_REVIEW_ENABLED:
                result = reviewer.run(date_str, apply=True)
                logger.info(
                    "AI 복기 완료: trades=%d decisions=%d journal=%s",
                    result["trade_count"],
                    result["decision_count"],
                    result["journal_path"],
                )
            report_done = True
            logger.info("EOD 리포트 전송 완료")

        # ── 장 중 스캔 ────────────────────────────────────────────────────────
        if _is_market_hours():
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
            # 종목 목록 30분마다 갱신
            if time.time() - last_stock_refresh > 1800:
                stocks = _load_stocks(args.top)
                last_stock_refresh = time.time()

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
            time.sleep(30)


if __name__ == "__main__":
    main()
