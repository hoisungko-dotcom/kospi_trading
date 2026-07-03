from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from collector.kiwoom_client import get_basic_price

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def persist_realtime_metrics(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def eod_marker_path(bot_root: Path, date_str: str) -> Path:
    return bot_root / "data" / f"eod_done_{date_str}.json"


def load_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def mark_eod_done(bot_root: Path, date_str: str, payload: dict) -> None:
    marker = eod_marker_path(bot_root, date_str)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def has_eod_done(bot_root: Path, date_str: str) -> bool:
    return eod_marker_path(bot_root, date_str).exists()


def review_telegram_message(
    date_str: str,
    review_result: dict | None,
    env_changed: bool,
    completed_at: str,
    *,
    signed_krw,
) -> str:
    if not review_result:
        return (
            f"🧠 *박스봇 복기 완료* {date_str}\n"
            f"  완료시각 {completed_at} KST\n"
            "  AI 복기는 비활성 상태였습니다."
        )

    learning_points = review_result.get("learning_points") or []
    top_lines = "\n".join(f"  - {point}" for point in learning_points[:4]) if learning_points else "  - 요약 생성 없음"
    restart_line = "예" if env_changed else "아니오"
    top_exit_reason = review_result.get("top_exit_reason") or "-"
    return (
        f"🧠 *박스봇 복기 완료* {date_str}\n"
        f"  완료시각 {completed_at} KST\n"
        f"  거래 {review_result.get('trade_count', 0)}건 | 승률 {review_result.get('win_rate', 0.0):.1f}%\n"
        f"  순손익 {signed_krw(review_result.get('net_pnl', 0))} | 기대값 {signed_krw(review_result.get('weighted_edge', 0))}\n"
        f"  주요 종료사유 {top_exit_reason} | 수정안 {review_result.get('decision_count', 0)}건 | 재시작필요 {restart_line}\n"
        f"  일지 {review_result.get('journal_path', '-')}\n"
        f"  학습핵심\n{top_lines}"
    )


def refresh_engine_from_broker(engine, broker, *, reason: str) -> bool:
    if not broker:
        return False
    balance = broker.get_balance()
    holdings = broker.get_holdings()
    if not balance:
        logger.warning("브로커 동기화 실패(%s): balance empty", reason)
        return False
    cash = float(balance.get("cash", 0) or 0)
    sync_ts = datetime.now(KST).strftime("%Y%m%d%H%M%S")
    engine.sync_from_broker(cash, holdings, sync_ts)
    logger.info("브로커 재동기화 완료(%s): 현금 %s | 보유 %d종목", reason, f"₩{cash:,.0f}", len(holdings))
    return True


def should_preserve_recent_local_positions(engine, holdings: list[dict], *, parse_kst_ts, grace_sec: int = 1800) -> list[str]:
    if holdings or not engine.positions:
        return []
    now = datetime.now(KST)
    preserved: list[str] = []
    for code, pos in engine.positions.items():
        try:
            elapsed = int((now - parse_kst_ts(pos.entry_ts)).total_seconds())
        except Exception:
            elapsed = grace_sec + 1
        if elapsed <= grace_sec:
            preserved.append(f"{pos.name}({code})/{elapsed}s")
    return preserved


def reconcile_broker_state(
    engine,
    broker,
    exit_managers: dict,
    *,
    reason: str,
    is_excluded_name,
    send_once_per_key,
) -> tuple[bool, list[str]]:
    if not broker:
        return False, []

    balance = broker.get_balance()
    holdings = broker.get_holdings()
    if not balance:
        logger.warning("브로커 재동기화 실패(%s): balance empty", reason)
        return False, []

    preserved_recent = should_preserve_recent_local_positions(
        engine,
        holdings,
        parse_kst_ts=lambda ts: datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=KST),
    )
    if preserved_recent:
        logger.warning(
            "브로커 재동기화 보류(%s): broker 보유 0건이지만 최근 로컬 포지션 보존 %d건: %s",
            reason,
            len(preserved_recent),
            ", ".join(preserved_recent[:8]),
        )
        return False, []

    excluded_codes: list[str] = []
    excluded_labels: list[str] = []
    for item in holdings:
        blocked, blocked_reason = is_excluded_name(item.get("name", item.get("code", "")))
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
                send_once_per_key(
                    f"excluded_liquidation_fail:{code}",
                    f"⚠️ *박스봇 전략 외 잔고 청산 실패* {name}({code})\n"
                    f"  수량 {qty}주\n"
                    f"  브로커 사유: {broker_reason}",
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
            blocked, blocked_reason = is_excluded_name(item.get("name", item.get("code", "")))
            if blocked:
                excluded_labels.append(f"{item.get('name', item.get('code', ''))}({item.get('code', '')})/{blocked_reason}")

    return True, excluded_labels


def force_close_end_of_day(
    engine,
    broker,
    exit_managers: dict,
    delay: float,
    *,
    signed_krw,
    send,
) -> int:
    closed_count = 0
    for code in list(engine.positions.keys()):
        pos = engine.positions.get(code)
        if not pos:
            continue

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

        trade = engine.sell(code, price, ts, "eod_force")
        if not trade:
            time.sleep(delay)
            continue

        closed_count += 1
        exit_managers.pop(code, None)
        emoji = "✅" if trade.pnl_krw >= 0 else "❌"
        logger.info("%s EOD 강제청산 %s(%s) | 수익 %s (%.2f%%)", emoji, trade.name, code, signed_krw(trade.pnl_krw), trade.pnl_pct)
        send(
            f"{emoji} *EOD 강제청산* {trade.name}({code})\n"
            f"  수익 {signed_krw(trade.pnl_krw)} ({trade.pnl_pct:+.2f}%) [eod_force]\n"
        )
        time.sleep(delay)
    return closed_count
