from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from collector.kiwoom_client import get_basic_price, get_min_chart, parse_candle
from collector.surge_detector import Candle
from realtime.kiwoom_realtime import RealtimeTick
from realtime.realtime_strategy import BoxRealtimeState

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def watch_candidate_from_candles(box_checker, candles: list[Candle], stk_cd: str, name: str) -> BoxRealtimeState | None:
    if len(candles) < box_checker.min_length + 2:
        return None
    current = candles[-1]
    box_window = candles[-(box_checker.max_length + 2):-1]
    box = box_checker._find_box(box_window)
    if box is None:
        return None
    rising_ratio = box_checker._rising_low_ratio(box["candles"])
    is_rising = rising_ratio >= box_checker.rising_thresh
    if box_checker.require_rising and not is_rising:
        return None
    daily_pass = True
    if box_checker.daily_enabled and stk_cd:
        daily_pass, _ = box_checker._check_daily(stk_cd)
        if not daily_pass:
            return None
    return BoxRealtimeState(
        code=stk_cd,
        name=name,
        box_high=box["box_high"],
        box_low=box["box_low"],
        preferred=box["preferred"],
        daily_pass=daily_pass,
        box_height_pct=box["height_pct"],
        box_length=box["length"],
        status="box_building",
        last_transition_ts=current.ts,
    )


def build_realtime_watchlist(stocks: list[dict], box_checker, rt_client, delay: float, watchlist_rank, watchlist_max: int) -> dict[str, BoxRealtimeState]:
    build_started = time.time()
    candidates: list[tuple[dict, BoxRealtimeState]] = []
    for stk in stocks:
        stk_cd = stk.get("code", "")
        name = stk.get("name", stk_cd)
        try:
            rows = get_min_chart(stk_cd, tic_scope="1", max_pages=1)
        except Exception:
            time.sleep(delay)
            continue
        if len(rows) < 6:
            time.sleep(delay)
            continue
        candles = [Candle(**parse_candle(r)) for r in reversed(rows)]
        state = watch_candidate_from_candles(box_checker, candles, stk_cd, name)
        if not state:
            time.sleep(delay)
            continue
        last_price = candles[-1].close
        distance_pct = ((state.box_high - last_price) / state.box_high) if state.box_high > 0 else 99.0
        candidates.append(({"code": stk_cd, "preferred_box": state.preferred, "distance_pct": distance_pct, "box_height_pct": state.box_height_pct, "box_length": state.box_length}, state))
        time.sleep(delay)

    candidates.sort(key=lambda item: watchlist_rank(item[0]), reverse=True)
    selected = candidates[:watchlist_max]
    watchlist = {state.code: state for _, state in selected}
    for code in rt_client.subscribed_codes():
        if code not in watchlist:
            rt_client.unsubscribe(code)
    for code in watchlist:
        rt_client.subscribe(code)
    logger.info("실시간 watchlist 재구성: %d종목 / 후보 %d건 (%.1fs)", len(watchlist), len(candidates), time.time() - build_started)
    return watchlist


def monitor_degraded_holdings_once(engine, broker, delay: float, *, signed_krw, send, stats_line, get_min_chart_fn=get_min_chart, parse_candle_fn=parse_candle) -> dict[str, float]:
    latest_prices: dict[str, float] = {}
    for code in list(engine.positions.keys()):
        pos = engine.positions.get(code)
        if not pos:
            continue
        try:
            rows = get_min_chart_fn(code, tic_scope="1", max_pages=1)
        except Exception:
            time.sleep(delay)
            continue
        if not rows:
            time.sleep(delay)
            continue
        candles = [Candle(**parse_candle_fn(r)) for r in reversed(rows)]
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
        trade = engine.sell(code, latest.close, latest.ts, reason)
        if trade:
            emoji = "✅" if trade.pnl_krw >= 0 else "❌"
            logger.info("%s degraded 청산 %s(%s) | 수익 %s (%.2f%%) [%s]", emoji, trade.name, code, signed_krw(trade.pnl_krw), trade.pnl_pct, reason)
            send(
                f"{emoji} *degraded 청산* {trade.name}({code})\n"
                f"  수익 {signed_krw(trade.pnl_krw)} ({trade.pnl_pct:+.2f}%) [{reason}]\n"
                f"  {stats_line(engine)}"
            )
        time.sleep(delay)
    return latest_prices


def refresh_stale_quotes_with_rest(rt_client, watchlist: dict[str, BoxRealtimeState], stale_codes: set[str], runtime_metrics: dict, *, get_basic_price_fn=get_basic_price) -> int:
    refreshed = 0
    for code in sorted(stale_codes):
        if code not in watchlist:
            continue
        try:
            snapshot = get_basic_price_fn(code)
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


def handle_realtime_exit(tick, engine, broker, runtime_metrics: dict, *, signed_krw, send) -> bool:
    reason = engine.tick(tick.code, tick.price, tick.ts, source="realtime")
    if not reason:
        return False
    if reason == "follow_through_fail":
        runtime_metrics["follow_through_fail_count"] += 1
    if broker:
        pos = engine.positions.get(tick.code)
        qty = pos.qty if pos else 0
        if qty > 0:
            order, confirmed = broker.sell_and_confirm(tick.code, qty)
            if not order or not confirmed:
                broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
                logger.warning("실시간 broker 매도 실패/대기: %s(%s) [%s] %s", tick.code, tick.code, reason, broker_reason)
                return False
    trade = engine.sell(tick.code, tick.price, tick.ts, reason)
    if not trade:
        return False
    emoji = "✅" if trade.pnl_krw >= 0 else "❌"
    logger.info("%s 실시간 청산 %s(%s) | 수익 %s (%.2f%%) [%s]", emoji, trade.name, tick.code, signed_krw(trade.pnl_krw), trade.pnl_pct, reason)
    send(
        f"{emoji} *실시간 청산* {trade.name}({tick.code})\n"
        f"  수익 {signed_krw(trade.pnl_krw)} ({trade.pnl_pct:+.2f}%) [{reason}]"
    )
    return True


def can_open_realtime_trade(ts: str, *, now_hhmm, time_policy, no_buy_before: str, recent_entry_times: list, parse_kst_ts, prune_recent_entries, max_new_buys_per_scan: int, max_new_buys_per_10min: int, is_opening_window, opening_max_buys_per_scan: int, opening_max_buys_per_10min: int) -> tuple[bool, str]:
    hhmm = ts[8:12] if len(ts) >= 12 else now_hhmm()
    if hhmm >= "1500":
        return False, "market_cutoff"
    if time_policy(hhmm) == "blocked":
        return False, f"blocked_before:{no_buy_before}"
    latest_dt = parse_kst_ts(ts)
    prune_recent_entries(latest_dt)
    scan_limit = max_new_buys_per_scan
    ten_min_limit = max_new_buys_per_10min
    if is_opening_window(hhmm):
        scan_limit = min(scan_limit, opening_max_buys_per_scan)
        ten_min_limit = min(ten_min_limit, opening_max_buys_per_10min)
    same_minute = [t for t in recent_entry_times if t.strftime("%Y%m%d%H%M") == latest_dt.strftime("%Y%m%d%H%M")]
    if len(same_minute) >= scan_limit:
        return False, f"signal_window_limit:{scan_limit}"
    if len(recent_entry_times) >= ten_min_limit:
        return False, f"ten_min_buy_limit:{ten_min_limit}"
    return True, "ok"


def handle_realtime_entry(tick, rt_state, engine, broker, state_machine, runtime_metrics: dict, *, can_open_trade, recent_entry_times: list, parse_kst_ts, signed_krw, send, stats_line) -> bool:
    allowed, reason = can_open_trade(tick.ts)
    if not allowed:
        logger.info("실시간 매수차단 %s(%s) | 사유=%s", rt_state.name, tick.code, reason)
        return False
    can_buy, buy_reason, qty, _ = engine.can_buy(tick.code, tick.price)
    if not can_buy:
        logger.info("실시간 매수차단 %s(%s) | 사유=%s", rt_state.name, tick.code, buy_reason)
        return False
    if broker:
        order = broker.buy(tick.code, qty)
        if not order:
            broker_reason = broker.last_reject_message or broker.last_error_message or "unknown"
            logger.warning("실시간 broker 매수 실패: %s(%s) | %s", rt_state.name, tick.code, broker_reason)
            return False
    ok = engine.buy(tick.code, rt_state.name, tick.price, tick.ts, box_high=rt_state.box_high, box_low=rt_state.box_low)
    if not ok:
        return False
    recent_entry_times.append(parse_kst_ts(tick.ts))
    state_machine.mark_holding(rt_state, tick.ts)
    logger.info("⚡ 실시간 매수 %s(%s) | 가격 %s | BOX 높이 %.2f%% 길이 %d봉 선호=%s", rt_state.name, tick.code, f"{tick.price:,.0f}원", rt_state.box_height_pct, rt_state.box_length, rt_state.preferred)
    send(
        f"⚡ *실시간 매수* {rt_state.name}({tick.code})\n"
        f"  진입가 {tick.price:,.0f}원 시각 {tick.ts[8:12]}\n"
        f"  BOX {rt_state.box_height_pct:.2f}% {rt_state.box_length}분봉 선호={rt_state.preferred}\n"
        f"  {stats_line(engine)}"
    )
    runtime_metrics["entry_filled_count"] += 1
    return True
