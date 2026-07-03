"""
전략 상태 엔진 — 포지션/잔액/거래내역 관리.
실전 모드에서는 브로커 잔고와 동기화된 전략 상태 파일을 사용한다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def _default_state_path() -> Path:
    mode = (os.getenv("KIS_TRADING_MODE", "mock").strip().lower() or "mock")
    filename = "live_strategy_state.json" if mode == "live" else "paper_state.json"
    return Path(__file__).parents[1] / "data" / filename


STATE_PATH = _default_state_path()

INITIAL_CASH    = 10_000_000   # ₩10,000,000
PER_TRADE_KRW   = int(os.getenv("BOX_BOT_PER_TRADE_KRW", "5000000") or 5000000)
MAX_POSITIONS   = int(os.getenv("BOX_BOT_MAX_POSITIONS", "2") or 2)
BUY_BUFFER_PCT  = float(os.getenv("BOX_BOT_BUY_BUFFER_PCT", "0.97") or 0.97)
BUY_FEE_PCT     = float(os.getenv("BOX_BOT_BUY_FEE_PCT", "0.00015") or 0.00015)
SELL_FEE_PCT    = float(os.getenv("BOX_BOT_SELL_FEE_PCT", "0.00015") or 0.00015)


def _env(name: str, legacy: str, default: str) -> str:
    return os.getenv(name, os.getenv(legacy, default))


EXIT_CANDLES = int(_env("BOX_BOT_EXIT_CANDLES", "PATTERN_EXIT_CANDLES", "2") or 2)
EXIT_MAX_CANDLES = int(_env("BOX_BOT_EXIT_MAX_CANDLES", "PATTERN_EXIT_MAX_CANDLES", "3") or 3)
EXTEND_ON_PROFIT = _env("BOX_BOT_EXTEND_ON_PROFIT", "PATTERN_EXTEND_ON_PROFIT", "true").lower() not in {"0", "false", "no"}
TRAILING_STOP_ENABLED = _env("BOX_BOT_TRAILING_STOP_ENABLED", "PATTERN_TRAILING_STOP_ENABLED", "true").lower() not in {"0", "false", "no"}
TRAILING_ARM_PCT = float(_env("BOX_BOT_TRAILING_ARM_PCT", "PATTERN_TRAILING_ARM_PCT", "0.003") or 0.003)
TRAILING_GAP_PCT = float(_env("BOX_BOT_TRAILING_GAP_PCT", "PATTERN_TRAILING_GAP_PCT", "0.007") or 0.007)
FOLLOW_THROUGH_ENABLED = _env("BOX_BOT_FOLLOW_THROUGH_ENABLED", "", "false").lower() in {"1", "true", "yes", "on"}
FOLLOW_THROUGH_BARS = int(_env("BOX_BOT_FOLLOW_THROUGH_BARS", "", "4") or 4)
FOLLOW_THROUGH_MIN_GAIN_PCT = float(_env("BOX_BOT_FOLLOW_THROUGH_MIN_GAIN_PCT", "", "0.003") or 0.003)
BOX_RT_FOLLOW_THROUGH_WINDOW_SEC = int(_env("BOX_RT_FOLLOW_THROUGH_WINDOW_SEC", "", "240") or 240)
BOX_RT_FOLLOW_THROUGH_RECLAIM_SEC = int(_env("BOX_RT_FOLLOW_THROUGH_RECLAIM_SEC", "", "20") or 20)
TIME_STOP_ENABLED = _env("BOX_BOT_TIME_STOP_ENABLED", "", "true").lower() not in {"0", "false", "no"}
TIME_STOP_BARS    = int(_env("BOX_BOT_TIME_STOP_BARS", "", "30") or 30)
BOX_BREAKOUT_FAIL_EXIT_PCT = float(_env("BOX_BREAKOUT_FAIL_EXIT_PCT", "", "0.001") or 0.001)
BOX_BREAKOUT_HARD_STOP_PCT = float(_env("BOX_BREAKOUT_HARD_STOP_PCT", "", "0.0025") or 0.0025)


@dataclass
class Position:
    code:        str
    name:        str
    entry_price: float
    qty:         int
    entry_ts:    str     # YYYYMMDDHHmmss
    entry_hour:  int     # 진입 시각(시)
    box_high:    float = 0.0
    box_low:     float = 0.0
    candles_held: int = 0
    peak_price:  float = 0.0
    realtime_ticks_held: int = 0
    last_tick_ts: str = ""
    entry_context: dict = field(default_factory=dict)


@dataclass
class Trade:
    code:        str
    name:        str
    entry_price: float
    exit_price:  float
    qty:         int
    pnl_pct:     float   # %
    pnl_krw:     int
    entry_ts:    str
    exit_ts:     str
    exit_reason: str     # "exit_2봉" | "stop_loss" | "eod_force"
    entry_hour:  int
    entry_fee_krw: int = 0
    exit_fee_krw: int = 0
    total_fee_krw: int = 0
    entry_context: dict = field(default_factory=dict)
    exit_context: dict = field(default_factory=dict)


class PaperEngine:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.per_trade_krw = PER_TRADE_KRW
        self.max_positions = MAX_POSITIONS
        self._metadata_upgrade_needed = False
        self._load()
        if self._metadata_upgrade_needed:
            self.save()

    def _load(self):
        if not self.path.exists():
            legacy_path = self.path.parent / "paper_state.json"
            if self.path.name == "live_strategy_state.json" and legacy_path.exists():
                try:
                    self.path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass
        if self.path.exists():
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self._metadata_upgrade_needed = "state_kind" not in d or "state_warning" not in d
            self.cash      = d.get("cash", INITIAL_CASH)
            self.positions = {
                k: Position(**{
                    "code": v["code"],
                    "name": v["name"],
                    "entry_price": v["entry_price"],
                    "qty": v["qty"],
                    "entry_ts": v["entry_ts"],
                    "entry_hour": v.get("entry_hour", 9),
                    "box_high": v.get("box_high", 0.0),
                    "box_low": v.get("box_low", 0.0),
                    "candles_held": v.get("candles_held", 0),
                    "peak_price": v.get("peak_price", v.get("entry_price", 0.0)),
                    "realtime_ticks_held": v.get("realtime_ticks_held", 0),
                    "last_tick_ts": v.get("last_tick_ts", ""),
                    "entry_context": v.get("entry_context", {}) or {},
                }) for k, v in d.get("positions", {}).items()
            }
            self.trades    = [Trade(**{
                "code": t["code"],
                "name": t["name"],
                "entry_price": t["entry_price"],
                "exit_price": t["exit_price"],
                "qty": t["qty"],
                "pnl_pct": t["pnl_pct"],
                "pnl_krw": t["pnl_krw"],
                "entry_ts": t["entry_ts"],
                "exit_ts": t["exit_ts"],
                "exit_reason": t["exit_reason"],
                "entry_hour": t.get("entry_hour", 9),
                "entry_fee_krw": t.get("entry_fee_krw", 0),
                "exit_fee_krw": t.get("exit_fee_krw", 0),
                "total_fee_krw": t.get("total_fee_krw", 0),
                "entry_context": t.get("entry_context", {}) or {},
                "exit_context": t.get("exit_context", {}) or {},
            }) for t in d.get("trades", [])]
        else:
            self.cash      = INITIAL_CASH
            self.positions = {}
            self.trades    = []

    def save(self):
        self.path.write_text(json.dumps({
            "state_kind": "live_strategy_state" if self.path.name == "live_strategy_state.json" else "legacy_paper_mode_state",
            "state_warning": "" if self.path.name == "live_strategy_state.json" else "legacy paper-mode state file; do not treat as live runtime truth",
            "cash":      self.cash,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "trades":    [asdict(t) for t in self.trades],
        }, ensure_ascii=False, indent=2))

    def sync_from_broker(self, cash: float, holdings: list[dict], ts: str) -> None:
        """실계좌/모의계좌 보유 상태를 로컬 엔진에 덮어쓴다.

        가능한 경우 기존 로컬 포지션의 진입시각/박스정보를 보존해
        주기 동기화가 entry_ts 를 현재 시각으로 덮어쓰지 않게 한다.
        """
        self.cash = float(cash)
        synced: dict[str, Position] = {}
        hour = int(ts[8:10]) if len(ts) >= 10 else 9
        for item in holdings:
            code = item["code"]
            entry_price = float(item.get("entry_price", 0) or 0)
            qty = int(item.get("qty", 0) or 0)
            if qty <= 0 or entry_price <= 0:
                continue
            current = float(item.get("current_price", entry_price) or entry_price)
            prev = self.positions.get(code)
            same_lot = (
                prev is not None
                and prev.qty == qty
                and abs(prev.entry_price - entry_price) < 1e-6
            )
            entry_ts = prev.entry_ts if same_lot else ts
            entry_hour = prev.entry_hour if same_lot else hour
            box_high = prev.box_high if same_lot else entry_price
            box_low = prev.box_low if same_lot else entry_price
            candles_held = prev.candles_held if same_lot else 0
            peak_price = max(prev.peak_price, current) if same_lot and prev else max(entry_price, current)
            synced[code] = Position(
                code=code,
                name=item.get("name", code),
                entry_price=entry_price,
                qty=qty,
                entry_ts=entry_ts,
                entry_hour=entry_hour,
                box_high=box_high,
                box_low=box_low,
                candles_held=candles_held,
                peak_price=peak_price,
                realtime_ticks_held=prev.realtime_ticks_held if same_lot and prev else 0,
                last_tick_ts=prev.last_tick_ts if same_lot and prev else "",
                entry_context=dict(prev.entry_context or {}) if same_lot and prev else {},
            )
        self.positions = synced
        self.save()

    def can_buy(self, code: str, price: float, *, size_multiplier: float = 1.0) -> tuple[bool, str, int, float]:
        if len(self.positions) >= self.max_positions:
            return False, f"slots_full:{len(self.positions)}/{self.max_positions}", 0, 0.0
        if code in self.positions:
            return False, "already_holding", 0, 0.0
        if price <= 0:
            return False, "invalid_price", 0, 0.0

        buffered_cash = max(0.0, float(self.cash) * BUY_BUFFER_PCT)
        if self.max_positions <= 1:
            max_budget = buffered_cash
        else:
            max_budget = min(float(self.per_trade_krw), buffered_cash)
        max_budget = max(0.0, max_budget * max(0.0, min(size_multiplier, 1.0)))
        qty = int(max_budget // price)
        if qty <= 0:
            return False, "qty_zero", 0, 0.0

        gross_cost = qty * price
        buy_fee = int(round(gross_cost * BUY_FEE_PCT))
        cost = gross_cost + buy_fee
        if cost > self.cash:
            return False, f"insufficient_cash:{int(self.cash):,}<{int(cost):,}", qty, cost
        return True, "ok", qty, cost

    # ── 매수 ──────────────────────────────────────────────────────────────────
    def buy(self, code: str, name: str, price: float, ts: str,
            box_high: float = 0.0, box_low: float = 0.0, qty_override: int | None = None,
            size_multiplier: float = 1.0, entry_context: dict | None = None) -> bool:
        if qty_override is None:
            ok, _, qty, cost = self.can_buy(code, price, size_multiplier=size_multiplier)
            if not ok:
                return False
        else:
            qty = int(qty_override)
            gross_cost = qty * price
            buy_fee = int(round(gross_cost * BUY_FEE_PCT))
            cost = gross_cost + buy_fee
            if qty <= 0:
                return False
            if len(self.positions) >= self.max_positions or code in self.positions or price <= 0:
                return False
            if cost > self.cash:
                return False

        self.cash -= cost
        hour = int(ts[8:10]) if len(ts) >= 10 else 9
        self.positions[code] = Position(
            code=code, name=name, entry_price=price, qty=qty, entry_ts=ts,
            entry_hour=hour, box_high=box_high, box_low=box_low, peak_price=price, last_tick_ts=ts,
            entry_context=dict(entry_context or {}),
        )
        self.save()
        return True

    # ── 매도 ──────────────────────────────────────────────────────────────────
    def sell(self, code: str, exit_price: float, exit_ts: str,
             reason: str, exit_context: dict | None = None) -> Trade | None:
        pos = self.positions.pop(code, None)
        if pos is None:
            return None

        gross_proceeds = pos.qty * exit_price
        exit_fee = int(round(gross_proceeds * SELL_FEE_PCT))
        proceeds = gross_proceeds - exit_fee
        gross_cost = pos.qty * pos.entry_price
        entry_fee = int(round(gross_cost * BUY_FEE_PCT))
        total_fee = entry_fee + exit_fee
        self.cash += proceeds
        pnl_krw = int(proceeds - (gross_cost + entry_fee))
        pnl_pct = round((exit_price - pos.entry_price) / pos.entry_price * 100, 3)

        trade = Trade(
            code=code, name=pos.name,
            entry_price=pos.entry_price, exit_price=exit_price,
            qty=pos.qty, pnl_pct=pnl_pct, pnl_krw=pnl_krw,
            entry_ts=pos.entry_ts, exit_ts=exit_ts,
            exit_reason=reason, entry_hour=pos.entry_hour,
            entry_fee_krw=entry_fee, exit_fee_krw=exit_fee, total_fee_krw=total_fee,
            entry_context=dict(pos.entry_context or {}),
            exit_context=dict(exit_context or {}),
        )
        self.trades.append(trade)
        self.save()
        return trade

    # ── 봉 경과 업데이트 + 자동 청산 판단 ─────────────────────────────────────
    def tick(self, code: str, current_price: float, ts: str, *, source: str = "bar") -> str | None:
        """
        매 1분봉마다 호출.
        returns: "box_breakout_fail" | "box_hard_stop" | "follow_through_fail" | "time_stop" | "trailing_stop" | None
        """
        pos = self.positions.get(code)
        if not pos:
            return None
        if source == "bar":
            pos.candles_held += 1
        else:
            pos.realtime_ticks_held += 1
        pos.last_tick_ts = ts
        if current_price > pos.peak_price:
            pos.peak_price = current_price

        if pos.box_high > 0:
            breakout_fail_floor = pos.box_high * (1.0 - BOX_BREAKOUT_FAIL_EXIT_PCT)
            if current_price <= breakout_fail_floor:
                return "box_breakout_fail"

        # 새 박스 규칙 손절: 박스 이탈 실패 또는 짧은 하드스탑만 사용.
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        if pnl_pct <= -BOX_BREAKOUT_HARD_STOP_PCT:
            return "box_hard_stop"

        peak_gain_pct = (pos.peak_price - pos.entry_price) / pos.entry_price

        if FOLLOW_THROUGH_ENABLED:
            if source == "bar":
                if pos.candles_held >= 2 and pos.box_high > 0 and current_price < pos.box_high:
                    return "follow_through_fail"
                if pos.candles_held >= FOLLOW_THROUGH_BARS and peak_gain_pct < FOLLOW_THROUGH_MIN_GAIN_PCT:
                    return "follow_through_fail"
            else:
                try:
                    elapsed_sec = int((datetime.strptime(ts, "%Y%m%d%H%M%S") - datetime.strptime(pos.entry_ts, "%Y%m%d%H%M%S")).total_seconds())
                except Exception:
                    elapsed_sec = 0
                if elapsed_sec >= BOX_RT_FOLLOW_THROUGH_RECLAIM_SEC and pos.box_high > 0 and current_price < pos.box_high:
                    return "follow_through_fail"
                if elapsed_sec >= BOX_RT_FOLLOW_THROUGH_WINDOW_SEC and peak_gain_pct < FOLLOW_THROUGH_MIN_GAIN_PCT:
                    return "follow_through_fail"

        # 타임스톱: N분 경과 후 ARM 미달 → 청산
        if TIME_STOP_ENABLED and source == "bar":
            if pos.candles_held >= TIME_STOP_BARS and peak_gain_pct < TRAILING_ARM_PCT:
                return "time_stop"

        # 트레일링 손절: 수익이 조금이라도 난 뒤 고점 대비 gap 이탈
        if TRAILING_STOP_ENABLED:
            trail_floor = pos.peak_price * (1.0 - TRAILING_GAP_PCT)
            if peak_gain_pct >= TRAILING_ARM_PCT and current_price <= trail_floor:
                return "trailing_stop"

        return None

    # ── 장 마감 강제 청산 ───────────────────────────────────────────────────────
    def force_close_all(self, prices: dict[str, float], ts: str) -> list[Trade]:
        closed = []
        for code in list(self.positions.keys()):
            price = prices.get(code, self.positions[code].entry_price)
            t = self.sell(code, price, ts, "eod_force")
            if t:
                closed.append(t)
        return closed

    # ── 당일 거래 요약 ─────────────────────────────────────────────────────────
    def today_summary(self, date_str: str) -> dict:
        today = [t for t in self.trades if t.entry_ts[:8] == date_str]
        if not today:
            return {"거래수": 0, "총손익": 0, "승률": 0.0}

        wins     = [t for t in today if t.pnl_krw > 0]
        total_pnl = sum(t.pnl_krw for t in today)

        # 시간대별
        hour_pnl: dict[int, list[int]] = {}
        for t in today:
            hour_pnl.setdefault(t.entry_hour, []).append(t.pnl_krw)

        return {
            "거래수":   len(today),
            "승율":     round(len(wins) / len(today) * 100, 1),
            "총손익":   total_pnl,
            "평균손익": round(total_pnl / len(today)),
            "시간대별": {str(h): sum(v) for h, v in sorted(hour_pnl.items())},
            "잔고":     int(self.cash),
            "거래목록": [
                {
                    "종목": t.name,
                    "수익": t.pnl_krw, "수익률": f"{t.pnl_pct:+.2f}%",
                    "사유": t.exit_reason, "진입": t.entry_ts[8:12],
                }
                for t in today
            ],
        }

    def reset_today(self, date_str: str):
        """오늘 거래내역만 초기화 (잔고·포지션은 유지)."""
        self.trades = [t for t in self.trades if t.entry_ts[:8] != date_str]
        self.save()

    def cumulative_stats(self) -> dict[str, int | float]:
        realized_profit = sum(t.pnl_krw for t in self.trades if t.pnl_krw > 0)
        realized_loss = sum(-t.pnl_krw for t in self.trades if t.pnl_krw < 0)
        net = realized_profit - realized_loss
        wins = sum(1 for t in self.trades if t.pnl_krw > 0)
        total = len(self.trades)
        return {
            "realized_profit": int(realized_profit),
            "realized_loss": int(realized_loss),
            "net_pnl": int(net),
            "total_fees": int(sum(t.total_fee_krw for t in self.trades)),
            "trade_count": total,
            "win_rate": round((wins / total) * 100, 1) if total else 0.0,
            "positions": len(self.positions),
            "cash": int(self.cash),
        }
