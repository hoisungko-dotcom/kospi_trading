from __future__ import annotations

import logging
from datetime import datetime

from collector.surge_detector import Candle

logger = logging.getLogger(__name__)


def handle_position_scan(
    *,
    stk_cd: str,
    name: str,
    latest,
    candles: list[Candle],
    engine,
    broker,
    exit_managers: dict,
    delay: float,
    refresh_engine_from_broker,
    send_once_per_key,
    send,
    signed_krw,
    stats_line,
    box_ladder_exit_cls,
) -> bool:
    if stk_cd not in engine.positions:
        return False
    pos = engine.positions[stk_cd]
    if stk_cd not in exit_managers:
        exit_managers[stk_cd] = box_ladder_exit_cls(
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
    if not reason:
        return True

    if broker:
        qty = engine.positions[stk_cd].qty
        order, confirmed = broker.sell_and_confirm(stk_cd, qty)
        if not order:
            broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
            if "잔고내역이 없습니다" in broker_reason:
                refreshed = refresh_engine_from_broker(engine, broker, reason=f"sell-no-holding:{stk_cd}")
                if refreshed and stk_cd not in engine.positions:
                    logger.info("브로커 기준 이미 청산된 종목 감지: %s(%s)", name, stk_cd)
                    exit_managers.pop(stk_cd, None)
                    return True
            logger.warning("브로커 모의매도 실패로 로컬 청산 생략: %s(%s) [%s] | %s", name, stk_cd, reason, broker_reason)
            send_once_per_key(
                f"broker_mock_sell_fail:{stk_cd}:{reason}",
                f"⚠️ *박스봇 모의매도 실패* {name}({stk_cd})\n"
                f"  사유 {reason}\n"
                f"  브로커 사유: {broker_reason}\n"
                f"  로컬 청산도 생략됨\n"
                f"  {stats_line(engine)}",
            )
            return True
        if not confirmed:
            refresh_engine_from_broker(engine, broker, reason=f"sell-pending:{stk_cd}")
            if stk_cd in engine.positions:
                logger.warning("브로커 모의매도 반영 대기: %s(%s) [%s]", name, stk_cd, reason)
                return True

    trade = engine.sell(stk_cd, latest.close, latest.ts, reason)
    if trade:
        exit_managers.pop(stk_cd, None)
        emoji = "✅" if trade.pnl_krw >= 0 else "❌"
        logger.info("%s 청산 %s(%s) | 수익 %s (%.2f%%) [%s]", emoji, trade.name, stk_cd, signed_krw(trade.pnl_krw), trade.pnl_pct, reason)
        send(
            f"{emoji} *청산* {trade.name}({stk_cd})\n"
            f"  수익 {signed_krw(trade.pnl_krw)} ({trade.pnl_pct:+.2f}%) [{reason}]\n"
            f"  {stats_line(engine)}"
        )
    return True


def collect_buy_candidate(
    *,
    stk_cd: str,
    name: str,
    latest,
    candles: list[Candle],
    box_checker,
    require_preferred_box: bool,
    is_opening_window,
    check_trend_rebreak,
    candidate_rank,
) -> dict | None:
    box_ok, box_info = box_checker.check(candles, stk_cd)
    if not box_ok:
        trend_ok, trend_info = check_trend_rebreak(candles)
        if trend_ok:
            logger.info(
                "추세재돌파 후보 %s(%s) | 눌림폭=%.2f%% 재돌파=%.2f%% 몸통=%.2f",
                name,
                stk_cd,
                trend_info["box_height_pct"],
                trend_info["breakout_close_pct"],
                trend_info["breakout_body_ratio"],
            )
            return {"code": stk_cd, "name": name, "latest": latest, "box_info": trend_info, "rank": candidate_rank(trend_info)}
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
        return None

    current_hhmm = latest.ts[8:12] if len(latest.ts) >= 12 else datetime.now().strftime("%H%M")
    require_preferred = require_preferred_box or is_opening_window(current_hhmm)
    if require_preferred and not box_info.get("preferred_box", False):
        logger.info(
            "매수차단 %s(%s) | 사유=require_preferred_box | BOX 높이 %.2f%% 길이=%d봉 선호=%s",
            name, stk_cd, box_info["box_height_pct"], box_info["box_length"], box_info["preferred_box"],
        )
        return None

    return {"code": stk_cd, "name": name, "latest": latest, "box_info": box_info, "rank": candidate_rank(box_info)}


def execute_buy_candidates(
    *,
    buy_candidates: list[dict],
    engine,
    broker,
    exit_managers: dict,
    recent_entry_times: list,
    parse_kst_ts,
    prune_recent_entries,
    time_policy,
    is_opening_window,
    max_new_buys_per_scan: int,
    max_new_buys_per_10min: int,
    opening_max_buys_per_scan: int,
    opening_max_buys_per_10min: int,
    no_buy_before: str,
    candidate_focus_pool,
    select_final_entry,
    stats_line,
    send,
    send_once_per_key,
    box_ladder_exit_cls,
) -> None:
    if not buy_candidates:
        return
    buy_candidates.sort(key=lambda item: item["rank"], reverse=True)
    buy_candidates = candidate_focus_pool(buy_candidates)
    logger.info("후보풀 %d종목: %s", len(buy_candidates), ", ".join(f"{c['name']}({c['code']})" for c in buy_candidates))
    final_entry = select_final_entry(buy_candidates)
    if final_entry is None:
        return
    buy_candidates = [final_entry]
    reference_dt = parse_kst_ts(buy_candidates[0]["latest"].ts)
    prune_recent_entries(reference_dt)
    reference_hhmm = buy_candidates[0]["latest"].ts[8:12]
    if time_policy(reference_hhmm) == "blocked":
        logger.info("신규진입 차단 구간: %s 이전은 진입 금지", no_buy_before)
        return
    scan_limit = max_new_buys_per_scan
    ten_min_limit = max_new_buys_per_10min
    if is_opening_window(reference_hhmm):
        scan_limit = min(scan_limit, opening_max_buys_per_scan)
        ten_min_limit = min(ten_min_limit, opening_max_buys_per_10min)

    new_buys_this_scan = 0
    for candidate in buy_candidates:
        stk_cd = candidate["code"]
        name = candidate["name"]
        latest = candidate["latest"]
        box_info = candidate["box_info"]
        latest_dt = parse_kst_ts(latest.ts)
        prune_recent_entries(latest_dt)
        if new_buys_this_scan >= scan_limit:
            logger.info("매수차단 %s(%s) | 사유=scan_buy_limit:%d", name, stk_cd, scan_limit)
            continue
        if len(recent_entry_times) >= ten_min_limit:
            logger.info("매수차단 %s(%s) | 사유=ten_min_buy_limit:%d", name, stk_cd, ten_min_limit)
            continue
        can_buy, buy_reason, qty, est_cost = engine.can_buy(stk_cd, latest.close)
        if not can_buy:
            logger.info("매수차단 %s(%s) | 사유=%s | 후보가 %.0f원 | 예상주문 %.0f원", name, stk_cd, buy_reason, latest.close, est_cost)
            continue
        if broker:
            broker_qty = broker.inquire_buyable_qty(stk_cd, market_order=True, price=latest.close)
            if broker_qty <= 0:
                broker_reason = broker.last_reject_message or broker.last_error_message or "broker_buyable_qty_zero"
                logger.warning("브로커 매수가능수량 0으로 진입 생략: %s(%s) | %s", name, stk_cd, broker_reason)
                send_once_per_key(
                    f"broker_mock_buyable_zero:{stk_cd}",
                    f"⚠️ *박스봇 매수가능수량 0* {name}({stk_cd})\n"
                    f"  후보가 {latest.close:,.0f}원\n"
                    f"  브로커 사유: {broker_reason}\n"
                    f"  {stats_line(engine)}",
                )
                continue
            if broker_qty < qty:
                logger.info("브로커 수량보정 %s(%s) | 로컬 %d주 -> 브로커 가능 %d주", name, stk_cd, qty, broker_qty)
                qty = broker_qty
            order = broker.buy(stk_cd, qty)
            if not order:
                broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                logger.warning("브로커 모의매수 실패로 로컬 매수 생략: %s(%s) | %s", name, stk_cd, broker_reason)
                send_once_per_key(
                    f"broker_mock_buy_fail:{stk_cd}",
                    f"⚠️ *박스봇 모의매수 실패* {name}({stk_cd})\n"
                    f"  신호는 발생했지만 브로커 모의주문 실패로 진입 생략\n"
                    f"  후보가 {latest.close:,.0f}원 / 수량 {qty}주\n"
                    f"  브로커 사유: {broker_reason}\n"
                    f"  {stats_line(engine)}",
                )
                continue
        ok = engine.buy(
            stk_cd,
            name,
            latest.close,
            latest.ts,
            box_high=box_info["box_high"],
            box_low=box_info["box_low"],
            qty_override=qty,
        )
        if not ok:
            continue
        new_buys_this_scan += 1
        recent_entry_times.append(latest_dt)
        exit_managers[stk_cd] = box_ladder_exit_cls(
            entry_price=latest.close,
            entry_box_high=box_info["box_high"],
            entry_box_low=box_info["box_low"],
            timeframe="1min",
        )
        logger.info(
            "⚡ 매수 %s(%s) | 전략=%s | 가격 %s | BOX 높이 %.2f%% 길이 %d봉 선호=%s 저점상승=%s 일봉=%s rank=%s focus_top=%d",
            name, stk_cd, box_info.get("strategy_type", "box"), f"{latest.close:,.0f}원",
            box_info["box_height_pct"], box_info["box_length"],
            box_info["preferred_box"], box_info["is_rising_lows"], box_info["daily_pass"],
            candidate["rank"], len(buy_candidates),
        )
        send(
            f"⚡ *매수* {name}({stk_cd})\n"
            f"  전략 {box_info.get('strategy_type', 'box')}\n"
            f"  진입가 {latest.close:,.0f}원  시각 {latest.ts[8:12]}\n"
            f"  BOX {box_info['box_height_pct']:.2f}% {box_info['box_length']}분봉 "
            f"선호={box_info['preferred_box']} 저점상승={box_info['is_rising_lows']}\n"
            f"  {stats_line(engine)}"
        )
