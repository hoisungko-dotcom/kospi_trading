from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from realtime.kiwoom_realtime import RealtimeQuoteState

KST = ZoneInfo("Asia/Seoul")


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default) or default)


def _env_int(name: str, default: str) -> int:
    return int(os.getenv(name, default) or default)


@dataclass
class BoxRealtimeState:
    code: str
    name: str
    box_high: float
    box_low: float
    preferred: bool
    daily_pass: bool | None
    box_height_pct: float
    box_length: int
    box_grade: str = "C"
    avg_box_volume: float = 0.0
    status: str = "box_building"
    status_reason: str = ""
    last_transition_ts: str = ""
    realtime_updates: int = 0
    stale_hits: int = 0
    cooldown_until_ts: str = ""
    breakout_watch_count: int = 0
    entry_pending_count: int = 0
    rejected_reasons: list[str] = field(default_factory=list)


class RealtimeEntryConfirmer:
    def __init__(self) -> None:
        self.near_breakout_pct = _env_float("BOX_RT_NEAR_BREAKOUT_PCT", "0.003")
        self.confirm_window_sec = _env_int("BOX_RT_CONFIRM_WINDOW_SEC", "20")
        self.min_trades = _env_int("BOX_RT_MIN_TRADES_IN_WINDOW", "5")
        self.min_volume = _env_int("BOX_RT_MIN_VOLUME_IN_WINDOW", "1500")
        self.min_breakout_pct = _env_float("BOX_RT_MIN_BREAKOUT_PCT", "0.0015")
        self.min_trade_intensity = _env_float("BOX_RT_MIN_TRADE_INTENSITY", "120.0")
        self.min_volume_ratio = _env_float("BOX_RT_MIN_VOLUME_RATIO_VS_BOX", "2.0")
        self.max_bid_ask_imbalance_floor = _env_float("BOX_RT_MIN_BID_ASK_IMBALANCE", "-0.15")
        self.max_spread_pct = _env_float("BOX_RT_MAX_SPREAD_PCT", "0.003")
        self.min_recent_price_change_pct = _env_float("BOX_RT_MIN_RECENT_PRICE_CHANGE_PCT", "0.0015")

    def is_near_breakout(self, state: BoxRealtimeState, quote: RealtimeQuoteState | None) -> bool:
        if not quote or state.box_high <= 0 or quote.last_price <= 0:
            return False
        threshold = state.box_high * (1.0 - self.near_breakout_pct)
        return quote.last_price >= threshold

    def confirm_entry(self, state: BoxRealtimeState, quote: RealtimeQuoteState | None) -> tuple[bool, str]:
        if not quote:
            return False, "missing_quote"
        if quote.last_price <= state.box_high:
            return False, "breakout_not_held"
        breakout_pct = ((quote.last_price - state.box_high) / state.box_high) if state.box_high > 0 else 0.0
        if quote.trade_velocity < self.min_trades:
            return False, "low_trade_velocity"
        if quote.cum_volume_delta < self.min_volume:
            return False, "low_trade_volume"
        if quote.bid_ask_imbalance < self.max_bid_ask_imbalance_floor:
            return False, "weak_bid_imbalance"
        if quote.best_bid > 0 and quote.best_ask > 0:
            spread_pct = (quote.best_ask - quote.best_bid) / quote.last_price if quote.last_price > 0 else 0.0
            if spread_pct > self.max_spread_pct:
                return False, "wide_spread"
        if len(quote.recent_prices) >= 3:
            first = float(quote.recent_prices[0] or 0.0)
            last = float(quote.recent_prices[-1] or 0.0)
            if first > 0:
                rise_pct = (last - first) / first
                if rise_pct < self.min_recent_price_change_pct:
                    return False, "weak_tick_followthrough"
        price_ok = breakout_pct >= self.min_breakout_pct
        volume_ratio = (quote.cum_volume_delta / state.avg_box_volume) if state.avg_box_volume > 0 else 0.0
        volume_ok = volume_ratio >= self.min_volume_ratio
        trade_intensity = quote.bid_ask_imbalance * 100.0 + 100.0
        intensity_ok = trade_intensity >= self.min_trade_intensity
        confirmations = sum((price_ok, volume_ok, intensity_ok))
        if confirmations < 3:
            return False, (
                f"weak_breakout_confirm:pct={breakout_pct:.4f},"
                f"vol_ratio={volume_ratio:.2f},intensity={trade_intensity:.1f}"
            )
        return True, (
            f"confirmed:pct={breakout_pct:.4f},vol_ratio={volume_ratio:.2f},"
            f"intensity={trade_intensity:.1f},grade={state.box_grade}"
        )


class RealtimeStateMachine:
    def __init__(self) -> None:
        self.confirm = RealtimeEntryConfirmer()
        self.cooldown_sec = _env_int("BOX_RT_COOLDOWN_SEC", "300")

    def update(self, state: BoxRealtimeState, quote: RealtimeQuoteState | None, *, now_ts: str) -> BoxRealtimeState:
        state.realtime_updates += 1
        if state.cooldown_until_ts and now_ts < state.cooldown_until_ts:
            state.status = "cooldown"
            return state

        if state.status in {"idle", "box_building"} and self.confirm.is_near_breakout(state, quote):
            state.status = "near_breakout"
            state.last_transition_ts = now_ts
            state.status_reason = "price_near_box_high"
            return state

        if state.status == "near_breakout":
            try:
                elapsed = int((datetime.strptime(now_ts, "%Y%m%d%H%M%S") - datetime.strptime(state.last_transition_ts, "%Y%m%d%H%M%S")).total_seconds())
            except Exception:
                elapsed = 0
            if quote and quote.last_price < state.box_high * (1.0 + self.confirm.min_breakout_pct):
                state.status = "box_building"
                state.status_reason = "breakout_not_held"
                return state
            if elapsed < self.confirm.confirm_window_sec:
                state.status_reason = f"confirm_wait:{elapsed}s"
                return state
            ok, reason = self.confirm.confirm_entry(state, quote)
            if ok:
                state.status = "entry_pending"
                state.entry_pending_count += 1
                state.last_transition_ts = now_ts
                state.status_reason = reason
            elif reason != "breakout_not_held":
                state.status = "breakout_watch"
                state.breakout_watch_count += 1
                state.status_reason = reason
            return state

        if state.status == "breakout_watch":
            try:
                elapsed = int((datetime.strptime(now_ts, "%Y%m%d%H%M%S") - datetime.strptime(state.last_transition_ts, "%Y%m%d%H%M%S")).total_seconds())
            except Exception:
                elapsed = self.confirm.confirm_window_sec
            if elapsed < self.confirm.confirm_window_sec:
                state.status_reason = f"confirm_wait:{elapsed}s"
                return state
            ok, reason = self.confirm.confirm_entry(state, quote)
            if ok:
                state.status = "entry_pending"
                state.entry_pending_count += 1
                state.last_transition_ts = now_ts
                state.status_reason = reason
            elif quote and quote.last_price < state.box_high:
                state.status = "near_breakout"
                state.status_reason = "retest_after_breakout_watch"
            else:
                state.rejected_reasons.append(reason)
            return state

        return state

    def mark_holding(self, state: BoxRealtimeState, ts: str) -> None:
        state.status = "holding"
        state.last_transition_ts = ts
        state.status_reason = "entry_filled"

    def mark_cooldown(self, state: BoxRealtimeState, ts: str, reason: str) -> None:
        state.status = "cooldown"
        state.status_reason = reason
        state.last_transition_ts = ts
        until = datetime.strptime(ts, "%Y%m%d%H%M%S") + timedelta(seconds=self.cooldown_sec)
        state.cooldown_until_ts = until.strftime("%Y%m%d%H%M%S")
