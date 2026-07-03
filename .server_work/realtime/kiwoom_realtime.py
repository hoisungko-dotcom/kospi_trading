from __future__ import annotations

import json
import logging
import os
import queue
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


@dataclass
class RealtimeTick:
    code: str
    event_type: str
    price: float
    volume: int
    ts: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RealtimeQuoteState:
    code: str
    last_price: float = 0.0
    last_trade_ts: str = ""
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_ask_imbalance: float = 0.0
    trade_velocity: int = 0
    cum_volume_delta: int = 0
    last_update_ts: float = 0.0
    stale_count: int = 0
    event_count: int = 0
    last_event_type: str = ""
    recent_prices: deque[float] = field(default_factory=lambda: deque(maxlen=12))
    recent_trades: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=48))


class KiwoomRealtimeClient:
    def __init__(self) -> None:
        self.enabled = _env_bool("BOX_RT_ENABLED", "false")
        self.mode = os.getenv("BOX_RT_MODE", "full")
        self.max_stale_sec = int(os.getenv("BOX_RT_MAX_STALE_SEC", "12") or 12)
        self.reconnect_backoff_sec = int(os.getenv("BOX_RT_RECONNECT_BACKOFF_SEC", "5") or 5)
        self.mock_messages = _env_bool("BOX_RT_TEST_MODE", "false")
        self.ws_url = os.getenv("KIWOOM_WS_URL", "wss://api.kiwoom.com:10000/api/dostk/websocket")
        self._status = "disabled" if not self.enabled else "degraded"
        self._states: dict[str, RealtimeQuoteState] = {}
        self._subscribed_codes: set[str] = set()
        self._event_q: queue.Queue[RealtimeTick] = queue.Queue()
        self._socket_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_connect_error = ""
        self._reconnect_attempts = 0
        self._transport = None
        self._event_count = 0
        self._ws = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_connect_error(self) -> str:
        return self._last_connect_error

    def connected(self) -> bool:
        return self._status == "connected"

    def degraded(self) -> bool:
        return self._status in {"degraded", "reconnecting", "disabled"}

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            import websocket  # type: ignore
        except Exception as exc:
            self._status = "degraded"
            self._last_connect_error = f"websocket dependency missing: {exc}"
            logger.warning("키움 실시간 비활성화: %s", self._last_connect_error)
            return

        self._transport = websocket
        self._status = "reconnecting"
        self._socket_thread = threading.Thread(target=self._run_socket_loop, daemon=True)
        self._socket_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def subscribe(self, code: str) -> None:
        with self._lock:
            self._subscribed_codes.add(code)
            self._states.setdefault(code, RealtimeQuoteState(code=code))
        if self.connected() and self._ws is not None:
            self._send_subscribe(self._ws, code)

    def unsubscribe(self, code: str) -> None:
        with self._lock:
            self._subscribed_codes.discard(code)
        if self.connected() and self._ws is not None:
            self._send_unsubscribe(self._ws, code)

    def get_state(self, code: str) -> RealtimeQuoteState | None:
        return self._states.get(code)

    def subscribed_codes(self) -> list[str]:
        with self._lock:
            return sorted(self._subscribed_codes)

    def poll_events(self, limit: int = 200) -> list[RealtimeTick]:
        items: list[RealtimeTick] = []
        for _ in range(limit):
            try:
                items.append(self._event_q.get_nowait())
            except queue.Empty:
                break
        return items

    def stale_codes(self, codes: list[str] | None = None) -> list[str]:
        now = time.time()
        targets = codes or list(self._states.keys())
        stale: list[str] = []
        for code in targets:
            state = self._states.get(code)
            if not state:
                continue
            if not state.last_update_ts or now - state.last_update_ts > self.max_stale_sec:
                stale.append(code)
        return stale

    def inject_event(self, tick: RealtimeTick) -> None:
        self._apply_tick(tick)
        self._event_q.put(tick)

    def stats_snapshot(self) -> dict[str, Any]:
        stale = self.stale_codes()
        return {
            "status": self._status,
            "subscribed_count": len(self._subscribed_codes),
            "stale_count": len(stale),
            "event_count": self._event_count,
            "reconnect_attempts": self._reconnect_attempts,
            "last_connect_error": self._last_connect_error,
        }

    def _run_socket_loop(self) -> None:
        keepalive_sec = int(os.getenv("BOX_RT_KEEPALIVE_SEC", "25") or 25)
        while not self._stop.is_set():
            try:
                ws = self._transport.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                ws.run_forever(
                    sslopt={"cert_reqs": ssl.CERT_NONE},
                    ping_interval=keepalive_sec,
                    ping_timeout=max(5, keepalive_sec // 2),
                )
            except Exception as exc:
                self._reconnect_attempts += 1
                self._last_connect_error = str(exc)
                self._status = "reconnecting"
                logger.warning("키움 실시간 재연결 대기: %s", exc)
            finally:
                self._ws = None
            if not self._stop.is_set():
                time.sleep(self.reconnect_backoff_sec)
        self._status = "degraded"

    def _on_open(self, ws) -> None:
        self._status = "connected"
        self._last_connect_error = ""
        self._send_auth(ws)

    def _on_message(self, ws, raw: Any) -> None:
        if self._handle_control_message(ws, raw):
            return
        for tick in self._parse_message(raw):
            self.inject_event(tick)

    def _on_error(self, ws, err) -> None:
        self._last_connect_error = str(err)
        self._status = "reconnecting"
        logger.warning("키움 실시간 에러: %s", err)

    def _on_close(self, ws, code, msg) -> None:
        if self._status == "connected":
            self._reconnect_attempts += 1
        self._status = "reconnecting"
        reason = f"{code} {msg}".strip()
        self._last_connect_error = reason or self._last_connect_error
        logger.warning("키움 실시간 종료: %s", reason or "no-close-reason")

    def _send_auth(self, ws) -> None:
        from collector.kiwoom_client import _get_token
        token = _get_token()
        payload = {"trnm": "LOGIN", "token": token}
        ws.send(json.dumps(payload))

    def _resubscribe(self, ws) -> None:
        codes = self.subscribed_codes()
        if not codes:
            return
        self._send_subscribe(ws, *codes, refresh="1")

    def _send_subscribe(self, ws, *codes: str, refresh: str = "0") -> None:
        if not codes:
            return
        payload = {
            "trnm": "REG",
            "grp_no": "1",
            "refresh": refresh,
            "data": [
                {"item": list(codes), "type": ["0B"]},
                {"item": list(codes), "type": ["0C"]},
            ],
        }
        ws.send(json.dumps(payload))

    def _send_unsubscribe(self, ws, code: str) -> None:
        payload = {
            "trnm": "REMOVE",
            "grp_no": "1",
            "data": [
                {"item": [code], "type": ["0B"]},
                {"item": [code], "type": ["0C"]},
            ],
        }
        try:
            ws.send(json.dumps(payload))
        except Exception:
            pass

    def _handle_control_message(self, ws, raw: Any) -> bool:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        trnm = str(payload.get("trnm") or "").strip().upper()
        if trnm == "PING":
            ws.send(json.dumps(payload))
            return True
        if trnm == "LOGIN":
            if payload.get("return_code") != 0:
                raise RuntimeError(f"키움 로그인 실패: {payload.get('return_msg')}")
            logger.info("키움 실시간 로그인 성공")
            self._resubscribe(ws)
            return True
        if trnm == "REG" and payload.get("return_code") not in (None, 0):
            raise RuntimeError(f"키움 실시간 등록 실패: {payload.get('return_msg')}")
        return False

    def _parse_message(self, raw: Any) -> list[RealtimeTick]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            message = json.loads(raw)
        except Exception:
            return []
        if isinstance(message, dict):
            tick = self._parse_dict_message(message)
            return [tick] if tick else []
        if isinstance(message, list):
            result = []
            for item in message:
                tick = self._parse_dict_message(item)
                if tick:
                    result.append(tick)
            return result
        return []

    def _parse_dict_message(self, payload: dict[str, Any]) -> RealtimeTick | None:
        trnm = str(payload.get("trnm") or payload.get("type") or "").strip()
        data = payload.get("data") or payload.get("result") or payload
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return None
        values = data.get("values")
        if isinstance(values, dict):
            merged = dict(values)
            merged.setdefault("item", data.get("item"))
            merged.setdefault("type", data.get("type"))
            data = merged
        code = str(data.get("item") or data.get("code") or data.get("stk_cd") or "").strip()
        if not code:
            code = str(data.get("9001") or "").strip()
        if not code:
            return None
        ts = str(data.get("cntr_tm") or data.get("trade_time") or data.get("timestamp") or data.get("908") or "")
        if len(ts) == 6:
            ts = datetime.now(KST).strftime("%Y%m%d") + ts
        price = float(str(data.get("cur_prc") or data.get("price") or data.get("close") or data.get("10") or 0).replace(",", "").lstrip("+-") or 0)
        volume = int(str(data.get("trde_qty") or data.get("volume") or data.get("15") or data.get("911") or 0).replace(",", "").lstrip("+-") or 0)
        best_bid = float(str(data.get("bid_pric") or data.get("best_bid") or data.get("buy_price") or data.get("28") or 0).replace(",", "").lstrip("+-") or 0)
        best_ask = float(str(data.get("ask_pric") or data.get("best_ask") or data.get("sell_price") or data.get("27") or 0).replace(",", "").lstrip("+-") or 0)
        return RealtimeTick(
            code=code,
            event_type=str(data.get("type") or trnm or "unknown"),
            price=price,
            volume=volume,
            ts=ts or datetime.now(KST).strftime("%Y%m%d%H%M%S"),
            best_bid=best_bid,
            best_ask=best_ask,
            meta=data,
        )

    def _apply_tick(self, tick: RealtimeTick) -> None:
        state = self._states.setdefault(tick.code, RealtimeQuoteState(code=tick.code))
        now = time.time()
        self._event_count += 1
        state.event_count += 1
        state.last_event_type = tick.event_type
        state.last_price = tick.price or state.last_price
        state.last_trade_ts = tick.ts or state.last_trade_ts
        state.best_bid = tick.best_bid or state.best_bid
        state.best_ask = tick.best_ask or state.best_ask
        state.last_update_ts = now
        state.recent_prices.append(state.last_price)
        if tick.volume > 0:
            state.recent_trades.append((now, tick.volume))
        while state.recent_trades and now - state.recent_trades[0][0] > 10:
            state.recent_trades.popleft()
        state.trade_velocity = len(state.recent_trades)
        state.cum_volume_delta = sum(v for _, v in state.recent_trades)
        denom = state.best_ask + state.best_bid
        state.bid_ask_imbalance = ((state.best_bid - state.best_ask) / denom) if denom > 0 else 0.0
