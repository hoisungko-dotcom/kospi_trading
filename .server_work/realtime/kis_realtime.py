"""
KIS 실전계좌 WebSocket 실시간 클라이언트
H0STCNT0 (주식체결) 구독 -> RealtimeTick 변환
KiwoomRealtimeClient 와 동일한 인터페이스 제공
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from realtime.kiwoom_realtime import RealtimeTick, RealtimeQuoteState

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

_REAL_BASE  = os.getenv("KIS_REAL_BASE_URL", "https://openapi.koreainvestment.com:9443")
_REAL_WS    = os.getenv("KIS_REAL_WS_URL",   "ws://ops.koreainvestment.com:21000")
_APPKEY     = os.getenv("KIS_REAL_APPKEY",    "")
_APPSECRET  = os.getenv("KIS_REAL_APPSECRET", "")
_TICK_AUDIT_ENABLED = os.getenv("BOX_RT_TICK_AUDIT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
_TICK_AUDIT_DIR = Path(os.getenv("BOX_RT_TICK_AUDIT_DIR", str(Path(__file__).parents[1] / "data" / "realtime_ticks")))


def _get_approval_key() -> str:
    r = requests.post(
        f"{_REAL_BASE}/oauth2/Approval",
        headers={"content-type": "application/json"},
        json={"grant_type": "client_credentials", "appkey": _APPKEY, "secretkey": _APPSECRET},
        timeout=10,
    )
    return r.json().get("approval_key", "")


# H0STCNT0 파이프 구분 필드 인덱스
_F_CODE   = 0
_F_TIME   = 1   # HHMMSS
_F_PRICE  = 2
_F_ASK    = 10  # 매도호가1
_F_BID    = 11  # 매수호가1
_F_VOL    = 12  # 체결거래량


def _parse_h0stcnt0(raw: str) -> RealtimeTick | None:
    """'0|H0STCNT0|1|code^time^price^...^ask^bid^vol^...' -> RealtimeTick"""
    try:
        parts = raw.split("|")
        if len(parts) < 4 or parts[1] != "H0STCNT0":
            return None
        fields = parts[3].split("^")
        code  = fields[_F_CODE]
        price = float(fields[_F_PRICE].lstrip("+-")) if len(fields) > _F_PRICE else 0.0
        vol   = int(fields[_F_VOL]) if len(fields) > _F_VOL else 0
        t     = fields[_F_TIME] if len(fields) > _F_TIME else ""
        ts    = datetime.now(KST).strftime("%Y%m%d") + t  # YYYYMMDDHHmmss
        ask   = float(fields[_F_ASK].lstrip("+-")) if len(fields) > _F_ASK and fields[_F_ASK] else 0.0
        bid   = float(fields[_F_BID].lstrip("+-")) if len(fields) > _F_BID and fields[_F_BID] else 0.0
        return RealtimeTick(code=code, event_type="trade", price=price, volume=vol, ts=ts,
                            best_ask=ask, best_bid=bid)
    except Exception:
        return None


class KisRealtimeClient:
    """KIS 실전 WebSocket -- KiwoomRealtimeClient 동일 인터페이스."""

    def __init__(self) -> None:
        self.enabled          = bool(_APPKEY and _APPSECRET)
        self._status          = "disabled" if not self.enabled else "disconnected"
        self._approval_key    = ""
        self._subscribed      : set[str]                    = set()
        self._states          : dict[str, RealtimeQuoteState] = {}
        self._event_q         : queue.Queue[RealtimeTick]   = queue.Queue()
        self._stop            = threading.Event()
        self._lock            = threading.Lock()
        self._ws              = None
        self._thread          : threading.Thread | None     = None
        self._event_count     = 0
        self._reconnect_count = 0
        self._last_error      = ""
        self._tick_audit_date = ""
        self._tick_audit_path: Path | None = None
        if _TICK_AUDIT_ENABLED:
            _TICK_AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    # -- 공개 API (KiwoomRealtimeClient 호환) ------------------------------------

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_connect_error(self) -> str:
        return self._last_error

    def connected(self) -> bool:
        return self._status == "connected"

    def degraded(self) -> bool:
        return self._status not in {"connected", "reconnecting", "disabled"}

    def start(self) -> None:
        if not self.enabled:
            logger.warning("KIS 실시간: APPKEY/APPSECRET 없음, 비활성")
            return
        try:
            import websocket as _ws_lib  # noqa: F401
        except ImportError:
            self._status = "degraded"
            return
        self._status = "reconnecting"
        self._thread = threading.Thread(target=self._socket_loop, daemon=True)
        self._thread.start()
        logger.info("KIS 실시간 클라이언트 시작")

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def subscribe(self, code: str) -> None:
        with self._lock:
            self._subscribed.add(code)
            self._states.setdefault(code, RealtimeQuoteState(code=code))
        if self.connected() and self._ws:
            self._send_subscribe(self._ws, code)

    def unsubscribe(self, code: str) -> None:
        with self._lock:
            self._subscribed.discard(code)

    def subscribed_codes(self) -> list[str]:
        with self._lock:
            return sorted(self._subscribed)

    def get_state(self, code: str) -> RealtimeQuoteState | None:
        return self._states.get(code)

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
        max_stale = int(os.getenv("BOX_RT_MAX_STALE_SEC", "12") or 12)
        return [
            c for c in targets
            if (state := self._states.get(c)) is None
            or not state.last_update_ts
            or now - state.last_update_ts > max_stale
        ]

    def inject_event(self, tick: RealtimeTick) -> None:
        st = self._states.get(tick.code)
        if st:
            now = time.time()
            st.last_price     = tick.price or st.last_price
            st.last_update_ts = now
            st.event_count   += 1
            # 체결거래량으로 trade_velocity / cum_volume_delta 추적
            if tick.volume > 0:
                st.recent_trades.append((now, tick.volume))
            while st.recent_trades and now - st.recent_trades[0][0] > 10:
                st.recent_trades.popleft()
            st.trade_velocity   = len(st.recent_trades)
            st.cum_volume_delta = sum(v for _, v in st.recent_trades)
            # 호가 갱신 (H0STCNT0 fields[10]/[11])
            if tick.best_ask or tick.best_bid:
                st.best_ask = tick.best_ask or st.best_ask
                st.best_bid = tick.best_bid or st.best_bid
                denom = st.best_ask + st.best_bid
                st.bid_ask_imbalance = ((st.best_bid - st.best_ask) / denom) if denom > 0 else 0.0
            st.recent_prices.append(st.last_price)
            st.last_trade_ts = tick.ts
            self._write_tick_audit(tick, st)
        self._event_count += 1
        self._event_q.put(tick)

    def stats_snapshot(self) -> dict:
        return {
            "status":            self._status,
            "subscribed_count":  len(self._subscribed),
            "stale_count":       len(self.stale_codes()),
            "event_count":       self._event_count,
            "reconnect_attempts": self._reconnect_count,
            "last_connect_error": self._last_error,
        }

    # -- 내부 소켓 루프 ----------------------------------------------------------

    def _socket_loop(self) -> None:
        import websocket as _ws_lib
        backoff = int(os.getenv("BOX_RT_RECONNECT_BACKOFF_SEC", "5") or 5)

        while not self._stop.is_set():
            try:
                self._approval_key = _get_approval_key()
                if not self._approval_key:
                    raise RuntimeError("접속키 발급 실패")

                ws = _ws_lib.WebSocketApp(
                    _REAL_WS,
                    on_open    = self._on_open,
                    on_message = self._on_message,
                    on_error   = self._on_error,
                    on_close   = self._on_close,
                )
                self._ws = ws
                logger.info("KIS WebSocket 연결 시도: %s", _REAL_WS)
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self._last_error = str(exc)
                self._reconnect_count += 1
                self._status = "reconnecting"
                logger.warning("KIS 실시간 재연결 대기: %s", exc)

            if not self._stop.is_set():
                time.sleep(backoff)

        self._status = "degraded"

    def _on_open(self, ws) -> None:
        self._status = "connected"
        self._last_error = ""
        logger.info("KIS WebSocket 연결 성공")
        for code in self.subscribed_codes():
            self._send_subscribe(ws, code)

    def _on_message(self, ws, raw: str) -> None:
        if "PINGPONG" in raw:
            try:
                ws.send(raw)
            except Exception:
                pass
            return

        if raw.startswith("0|") or raw.startswith("1|"):
            tick = _parse_h0stcnt0(raw)
            if tick:
                self.inject_event(tick)
            return

        try:
            d = json.loads(raw)
            rt  = d.get("body", {}).get("rt_cd", "")
            msg = d.get("body", {}).get("msg1", "")
            tr  = d.get("header", {}).get("tr_id", "")
            logger.debug("KIS WS 제어: tr=%s rt=%s msg=%s", tr, rt, msg)
        except Exception:
            pass

    def _on_error(self, ws, err) -> None:
        self._last_error = str(err)
        self._status = "reconnecting"
        logger.warning("KIS WebSocket 에러: %s", err)

    def _write_tick_audit(self, tick: RealtimeTick, state: RealtimeQuoteState) -> None:
        if not _TICK_AUDIT_ENABLED:
            return
        try:
            date_str = tick.ts[:8]
            if len(date_str) != 8:
                return
            if self._tick_audit_date != date_str or self._tick_audit_path is None:
                _TICK_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
                self._tick_audit_date = date_str
                self._tick_audit_path = _TICK_AUDIT_DIR / f"{date_str}.jsonl"
            payload = {
                "ts": tick.ts,
                "code": tick.code,
                "price": tick.price,
                "volume": tick.volume,
                "best_bid": tick.best_bid,
                "best_ask": tick.best_ask,
                "trade_velocity_10s": state.trade_velocity,
                "cum_volume_delta_10s": state.cum_volume_delta,
                "bid_ask_imbalance": round(state.bid_ask_imbalance, 6),
                "event_count": state.event_count,
            }
            with self._tick_audit_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("KIS tick audit write failed: %s", exc)

    def _on_close(self, ws, code, msg) -> None:
        if self._status == "connected":
            self._reconnect_count += 1
        self._status = "reconnecting"
        logger.info("KIS WebSocket 종료: %s %s", code, msg)

    def _send_subscribe(self, ws, code: str) -> None:
        msg = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
        }
        try:
            ws.send(json.dumps(msg))
            logger.debug("KIS 구독: %s", code)
        except Exception as e:
            logger.warning("KIS 구독 전송 실패 %s: %s", code, e)
