"""
페이퍼 트레이딩 엔진 — 포지션/잔액/거래내역 관리.
실제 주문 없음. 키움 1분봉 close를 체결가로 사용.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
STATE_PATH = Path(__file__).parents[1] / "data" / "paper_state.json"

INITIAL_CASH    = 10_000_000   # ₩10,000,000
PER_TRADE_KRW   = 1_000_000    # 1회 투자금
MAX_POSITIONS   = 5
STOP_LOSS_PCT   = -0.02        # -2% 손절


def _env(name: str, legacy: str, default: str) -> str:
    return os.getenv(name, os.getenv(legacy, default))


EXIT_CANDLES = int(_env("BOX_BOT_EXIT_CANDLES", "PATTERN_EXIT_CANDLES", "2") or 2)
EXIT_MAX_CANDLES = int(_env("BOX_BOT_EXIT_MAX_CANDLES", "PATTERN_EXIT_MAX_CANDLES", "3") or 3)
EXTEND_ON_PROFIT = _env("BOX_BOT_EXTEND_ON_PROFIT", "PATTERN_EXTEND_ON_PROFIT", "true").lower() not in {"0", "false", "no"}
TRAILING_STOP_ENABLED = _env("BOX_BOT_TRAILING_STOP_ENABLED", "PATTERN_TRAILING_STOP_ENABLED", "true").lower() not in {"0", "false", "no"}
TRAILING_ARM_PCT = float(_env("BOX_BOT_TRAILING_ARM_PCT", "PATTERN_TRAILING_ARM_PCT", "0.003") or 0.003)
TRAILING_GAP_PCT = float(_env("BOX_BOT_TRAILING_GAP_PCT", "PATTERN_TRAILING_GAP_PCT", "0.01") or 0.01)
FOLLOW_THROUGH_ENABLED = _env("BOX_BOT_FOLLOW_THROUGH_ENABLED", "", "false").lower() in {"1", "true", "yes", "on"}
FOLLOW_THROUGH_BARS = int(_env("BOX_BOT_FOLLOW_THROUGH_BARS", "", "4") or 4)
FOLLOW_THROUGH_MIN_GAIN_PCT = float(_env("BOX_BOT_FOLLOW_THROUGH_MIN_GAIN_PCT", "", "0.003") or 0.003)


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


class PaperEngine:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.per_trade_krw = PER_TRADE_KRW
        self._load()

    def _load(self):
        if self.path.exists():
            d = json.loads(self.path.read_text())
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
            }) for t in d.get("trades", [])]
        else:
            self.cash      = INITIAL_CASH
            self.positions = {}
            self.trades    = []

    def save(self):
        self.path.write_text(json.dumps({
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
            )
        self.positions = synced
        self.save()

    def can_buy(self, code: str, price: float) -> tuple[bool, str, int, float]:
        if len(self.positions) >= MAX_POSITIONS:
            return False, f"slots_full:{len(self.positions)}/{MAX_POSITIONS}", 0, 0.0
        if code in self.positions:
            return False, "already_holding", 0, 0.0
        if price <= 0:
            return False, "invalid_price", 0, 0.0

        qty = int(PER_TRADE_KRW // price)
        if qty <= 0:
            return False, "qty_zero", 0, 0.0

        cost = qty * price
        if cost > self.cash:
            return False, f"insufficient_cash:{int(self.cash):,}<{int(cost):,}", qty, cost
        return True, "ok", qty, cost

    # ── 매수 ──────────────────────────────────────────────────────────────────
    def buy(self, code: str, name: str, price: float, ts: str,
            box_high: float = 0.0, box_low: float = 0.0) -> bool:
        ok, _, qty, cost = self.can_buy(code, price)
        if not ok:
            return False

        self.cash -= cost
        hour = int(ts[8:10]) if len(ts) >= 10 else 9
        self.positions[code] = Position(
            code=code, name=name, entry_price=price, qty=qty, entry_ts=ts,
            entry_hour=hour, box_high=box_high, box_low=box_low, peak_price=price,
        )
        self.save()
        return True

    # ── 매도 ──────────────────────────────────────────────────────────────────
    def sell(self, code: str, exit_price: float, exit_ts: str,
             reason: str) -> Trade | None:
        pos = self.positions.pop(code, None)
        if pos is None:
            return None

        proceeds = pos.qty * exit_price
        self.cash += proceeds
        pnl_krw = int(proceeds - pos.qty * pos.entry_price)
        pnl_pct = round((exit_price - pos.entry_price) / pos.entry_price * 100, 3)

        trade = Trade(
            code=code, name=pos.name,
            entry_price=pos.entry_price, exit_price=exit_price,
            qty=pos.qty, pnl_pct=pnl_pct, pnl_krw=pnl_krw,
            entry_ts=pos.entry_ts, exit_ts=exit_ts,
            exit_reason=reason, entry_hour=pos.entry_hour,
        )
        self.trades.append(trade)
        self.save()
        return trade

    # ── 봉 경과 업데이트 + 자동 청산 판단 ─────────────────────────────────────
    def tick(self, code: str, current_price: float, ts: str) -> str | None:
        """
        매 1분봉마다 호출.
        returns: "stop_loss" | "trailing_stop" | None
        """
        pos = self.positions.get(code)
        if not pos:
            return None
        pos.candles_held += 1
        if current_price > pos.peak_price:
            pos.peak_price = current_price

        # 손절
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        if pnl_pct <= STOP_LOSS_PCT:
            return "stop_loss"

        peak_gain_pct = (pos.peak_price - pos.entry_price) / pos.entry_price

        if FOLLOW_THROUGH_ENABLED:
            if pos.candles_held >= 2 and pos.box_high > 0 and current_price < pos.box_high:
                return "follow_through_fail"
            if pos.candles_held >= FOLLOW_THROUGH_BARS and peak_gain_pct < FOLLOW_THROUGH_MIN_GAIN_PCT:
                return "follow_through_fail"

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
            "trade_count": total,
            "win_rate": round((wins / total) * 100, 1) if total else 0.0,
            "positions": len(self.positions),
            "cash": int(self.cash),
        }
