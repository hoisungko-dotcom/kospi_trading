"""
Kiwoom-only broker/client for the legacy box bot runtime.

Strategy logic stays in main.py. This adapter matches the small subset of the
broker client interface that the runtime actually consumes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

import pandas as pd
import requests

from brokers.kiwoom.auth import KiwoomAuthClient
from brokers.kiwoom.data_provider import KiwoomDataProvider

logger = logging.getLogger(__name__)


class KiwoomClientKospi:
    def __init__(self):
        self.broker_name = "kiwoom"
        self._auth = KiwoomAuthClient.from_env()
        self._provider = KiwoomDataProvider(self._auth)
        self._client = self
        self.is_mock = str(os.getenv("KIWOOM_ENV", "mock")).lower().startswith("mock")
        self.data_base_url = os.getenv("KIWOOM_BASE_URL", "https://mockapi.kiwoom.com").rstrip("/")
        self.trade_base_url = self.data_base_url
        self.trade_account = os.getenv("KIWOOM_ACCOUNT_NO", "").strip()
        self.trade_appkey = os.getenv("KIWOOM_APPKEY") or os.getenv("KIWOOM_APP_KEY") or ""
        self.trade_appsecret = os.getenv("KIWOOM_APPSECRET") or os.getenv("KIWOOM_APP_SECRET") or ""
        self.data_appkey = self.trade_appkey
        self.data_appsecret = self.trade_appsecret
        self._last_api_call_t = 0.0
        self._api_min_interval = float(os.getenv("KIWOOM_SERIAL_API_MIN_INTERVAL", "0.25") or 0.25)
        self._api_lock = Lock()
        self._response_cache: dict[tuple, tuple[float, object]] = {}
        self._tick_cache_ttl = float(os.getenv("KIWOOM_TICK_CACHE_TTL_SEC", "1.2") or 1.2)
        self._price_cache_ttl = float(os.getenv("KIWOOM_PRICE_CACHE_TTL_SEC", "1.0") or 1.0)
        self._minute_cache_ttl = float(os.getenv("KIWOOM_MINUTE_CACHE_TTL_SEC", "3.0") or 3.0)
        self._daily_cache_ttl = float(os.getenv("KIWOOM_DAILY_CACHE_TTL_SEC", "120.0") or 120.0)
        self._flow_cache_ttl = float(os.getenv("KIWOOM_FLOW_CACHE_TTL_SEC", "30.0") or 30.0)
        self._state_path = Path(__file__).parent.parent / "data" / "kiwoom_mock_account_state.json"
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_state()
        logger.info("✅ KiwoomClientKospi 초기화 완료 (%s투자)", "모의" if self.is_mock else "실전")

    def proactive_refresh_tokens(self):
        self._auth.token()

    @property
    def data_token(self) -> str | None:
        try:
            return self._auth.token()
        except Exception:
            return None

    def _api_wait(self, label: str = "") -> None:
        with self._api_lock:
            elapsed = time.time() - self._last_api_call_t
            if elapsed < self._api_min_interval:
                time.sleep(self._api_min_interval - elapsed)
            self._last_api_call_t = time.time()

    def _cache_get(self, key: tuple, ttl_sec: float):
        cached = self._response_cache.get(key)
        if not cached:
            return None
        saved_at, value = cached
        if time.time() - saved_at > ttl_sec:
            self._response_cache.pop(key, None)
            return None
        return value

    def _cache_put(self, key: tuple, value):
        self._response_cache[key] = (time.time(), value)
        return value

    def _ensure_state(self) -> None:
        if self._state_path.exists():
            return
        initial_cash = float(os.getenv("ACCOUNT_BALANCE", os.getenv("KIWOOM_MOCK_INITIAL_CASH", "10000000")) or 10000000)
        self._write_state({
            "timestamp": datetime.now().isoformat(),
            "cash": initial_cash,
            "holdings": {},
        })

    def _read_state(self) -> dict:
        self._ensure_state()
        return json.loads(self._state_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        state["timestamp"] = datetime.now().isoformat()
        self._state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _safe_float(self, val, default: float = 0.0) -> float:
        try:
            return float(str(val).replace(",", ""))
        except Exception:
            return default

    def _safe_int(self, val, default: int = 0) -> int:
        try:
            return int(float(str(val).replace(",", "")))
        except Exception:
            return default

    def _compute_indicators(self, df: pd.DataFrame, symbol: str = "") -> dict | None:
        try:
            if df is None or df.empty or len(df) < 20:
                return None
            close = df["close"].astype(float)
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            volume = df["volume"].astype(float)
            sma_5 = close.rolling(5).mean()
            sma_20 = close.rolling(20).mean()
            sma_52 = close.rolling(52).mean()
            sma_60 = close.rolling(60).mean()
            sma_224 = close.rolling(224).mean()
            sma_448 = close.rolling(448).mean()
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = -delta.clip(upper=0).rolling(14).mean()
            rsi = 100 - (100 / (1 + gain / loss.replace(0, float("nan"))))
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            bb_mid = close.rolling(20).mean()
            bb_upper = bb_mid + 2 * close.rolling(20).std()
            avg_vol = volume.rolling(20).mean()
            tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
            atr = tr.rolling(14).mean()

            def s(series, idx=-1, default=0.0):
                try:
                    v = series.iloc[idx]
                    return float(v) if pd.notna(v) else default
                except Exception:
                    return default

            return {
                "timestamp": df["date"].iloc[-1] if "date" in df.columns else "",
                "name": symbol,
                "close": s(close),
                "open": s(df["open"].astype(float)),
                "high": s(high),
                "low": s(low),
                "volume": s(volume, -2, default=s(volume)),
                "consecutive_buy_days": 0,
                "sma_5": s(sma_5, default=s(close)),
                "sma_20": s(sma_20, default=s(close)),
                "sma_52": s(sma_52),
                "sma_60": s(sma_60, default=s(close)),
                "sma_224": s(sma_224),
                "sma_224_prev": s(sma_224, -2),
                "sma_448": s(sma_448),
                "close_prev": s(close, -2, default=s(close)),
                "rsi": s(rsi, default=50.0),
                "rsi_prev": s(rsi, -2, default=50.0),
                "macd": s(macd_line),
                "macd_signal": s(signal_line),
                "macd_prev": s(macd_line, -2),
                "macd_signal_prev": s(signal_line, -2),
                "avg_volume_20": s(avg_vol, default=s(volume)),
                "bb_upper": s(bb_upper, default=s(close)),
                "bb_middle": s(bb_mid, default=s(close)),
                "close_5d_ago": s(close, -5, default=s(close)),
                "close_20d_ago": s(close, -20, default=s(close)),
                "high_20d": float(high.tail(20).max()),
                "low_52w": float(low.tail(252).min()) if len(low) >= 252 else float(low.min()),
                "atr": s(atr, default=s(close) * 0.02),
                "stop_loss": 0.0,
            }
        except Exception as exc:
            logger.debug("지표 계산 실패 (%s): %s", symbol, exc)
            return None

    def get_bulk_daily_ohlcv(self, symbols: list[str], kospi_set: set = None) -> list[dict]:
        results = []
        success = 0
        total = len(symbols)
        logger.info("📡 Kiwoom 일봉 수집 중... %d개", total)
        for idx, sym in enumerate(symbols, 1):
            data = self.get_daily_ohlcv(sym)
            if data is not None:
                success += 1
            results.append({"symbol": sym, "name": sym, "data": data, "error": None})
            if idx % 50 == 0 or idx == total:
                logger.info("  진행: %d/%d | 성공 %d개", idx, total, success)
        logger.info("✅ Kiwoom 일봉 수집 완료: %d/%d개 성공", success, total)
        return results

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        result: dict[str, float] = {}
        missing: list[str] = []
        for sym in symbols:
            cache_key = ("current_price", sym)
            cached = self._cache_get(cache_key, self._price_cache_ttl)
            if cached is None:
                missing.append(sym)
            else:
                result[sym] = cached
        if missing:
            self._api_wait("current_prices")
            fetched = self._provider.get_current_prices(missing) or {}
            for sym, price in fetched.items():
                if price:
                    result[sym] = self._cache_put(("current_price", sym), price)
        return result

    def get_top_trading_value_symbols(self, limit: int = 200, market: str = "001") -> list[dict]:
        cache_key = ("rank_trading_value", market, limit)
        cached = self._cache_get(cache_key, 20.0)
        if cached is not None:
            return cached
        self._api_wait("rank_trading_value")
        rows = self._provider.get_trading_value_rank(market=market) or []
        parsed: list[dict] = []
        for row in rows[:limit]:
            symbol = str(row.get("stk_cd") or row.get("symbol") or "").split("_")[0].zfill(6)
            if not symbol:
                continue
            parsed.append({
                "symbol": symbol,
                "name": row.get("stk_nm") or row.get("name") or symbol,
                "rank": self._safe_int(row.get("now_rank") or row.get("rank")),
                "trading_value": self._safe_float(row.get("trde_prica") or row.get("acc_trde_prica")),
                "raw": row,
            })
        return self._cache_put(cache_key, parsed)

    def get_top_net_buying_symbols(self, limit: int = 200, market: str = "001") -> list[dict]:
        cache_key = ("rank_net_buy", market, limit)
        cached = self._cache_get(cache_key, 20.0)
        if cached is not None:
            return cached
        self._api_wait("rank_net_buy")
        rows = self._provider.get_investor_net_buy_rank(market=market) or []
        combined: dict[str, dict] = {}
        for idx, row in enumerate(rows, start=1):
            candidates = [
                (
                    str(row.get("for_netprps_stk_cd") or "").zfill(6),
                    row.get("for_netprps_stk_nm"),
                    self._safe_float(row.get("for_netprps_amt")),
                    "foreign",
                ),
                (
                    str(row.get("orgn_netprps_stk_cd") or "").zfill(6),
                    row.get("orgn_netprps_stk_nm"),
                    self._safe_float(row.get("orgn_netprps_amt")),
                    "institution",
                ),
            ]
            for symbol, name, net_buy, source in candidates:
                if not symbol:
                    continue
                if net_buy <= 0:
                    continue
                prev = combined.get(symbol)
                if prev is None:
                    combined[symbol] = {
                        "symbol": symbol,
                        "name": name or symbol,
                        "rank": idx,
                        "net_buy": net_buy,
                        "source": source,
                        "raw": row,
                    }
                else:
                    prev["net_buy"] += net_buy
                    prev["source"] = "foreign+institution"

        parsed = sorted(combined.values(), key=lambda x: x["net_buy"], reverse=True)[:limit]
        for i, row in enumerate(parsed, start=1):
            row["rank"] = i
        return self._cache_put(cache_key, parsed)

    def get_daily_ohlcv(self, symbol: str) -> dict | None:
        cache_key = ("daily", symbol, 500)
        df = self._cache_get(cache_key, self._daily_cache_ttl)
        if df is None:
            self._api_wait("daily")
            df = self._provider.get_daily_bars(symbol, lookback=500)
            if df is not None:
                self._cache_put(cache_key, df)
        return self._compute_indicators(df, symbol) if df is not None else None

    def get_intraday_ohlcv(self, symbol: str, interval: str = "1m", lookback: int = 30):
        try:
            minutes = max(1, int(str(interval).replace("m", "").strip() or "1"))
        except Exception:
            minutes = 1
        cache_key = ("minute", symbol, minutes, lookback)
        cached = self._cache_get(cache_key, self._minute_cache_ttl)
        if cached is not None:
            return cached
        self._api_wait("intraday")
        df = self._provider.get_minute_bars_df(symbol, interval=minutes, lookback=lookback)
        if df is not None:
            self._cache_put(cache_key, df)
        return df

    def get_tick_ohlcv(self, symbol: str, tick_scope: int = 1, lookback: int = 60):
        cache_key = ("tick", symbol, tick_scope, lookback)
        cached = self._cache_get(cache_key, self._tick_cache_ttl)
        if cached is not None:
            return cached
        self._api_wait("tick")
        df = self._provider.get_tick_bars_df(symbol, tick_scope=tick_scope, lookback=lookback)
        if df is not None:
            self._cache_put(cache_key, df)
        return df

    def get_foreign_net_buying(self, symbol: str, lookback: int = 5) -> list[dict]:
        cache_key = ("flow", symbol, lookback)
        cached = self._cache_get(cache_key, self._flow_cache_ttl)
        if cached is not None:
            return cached
        self._api_wait("flow")
        url = self.data_base_url + "/api/dostk/frgnistt"
        headers = self._auth.auth_headers({"api-id": "ka10008", "cont-yn": "N", "next-key": ""})
        body = {"stk_cd": symbol.zfill(6)}
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("stk_frgnr") or data.get("output") or data.get("data") or []
            flow = []
            for row in rows[:lookback]:
                net = self._safe_float(row.get("chg_qty") or row.get("frgnr_ntby_qty") or row.get("net_flow"))
                flow.append({
                    "date": row.get("dt") or row.get("date") or "",
                    "foreigner_net": net,
                    "institution_net": 0.0,
                    "net_flow": net,
                })
            flow.sort(key=lambda x: x.get("date", ""))
            return self._cache_put(cache_key, flow)
        except Exception as exc:
            logger.debug("외국인 순매수 조회 실패 (%s): %s", symbol, exc)
            return []

    def get_orderable_cash(self, symbol: str, price: float, use_max: bool = False) -> float:
        state = self._read_state()
        return float(state.get("cash", 0) or 0)

    def get_balance(self) -> dict:
        state = self._read_state()
        holdings = {}
        for symbol, info in (state.get("holdings") or {}).items():
            qty = self._safe_int(info.get("quantity"))
            if qty <= 0:
                continue
            holdings[symbol] = {
                "quantity": qty,
                "price": self._safe_float(info.get("price")),
                "amount": self._safe_float(info.get("amount")),
                "highest_price": self._safe_float(info.get("highest_price") or info.get("price")),
            }
        cash = self._safe_float(state.get("cash"))
        logger.info("✅ Kiwoom 잔고 조회 완료: 예수금 ₩%s, 보유종목 %d개", f"{cash:,.0f}", len(holdings))
        return {"cash": cash, "holdings": holdings}

    def _submit_order(self, symbol: str, quantity: int, price: float, side: str, market_order: bool = False) -> bool:
        url = self.trade_base_url + "/api/dostk/ordr"
        api_id = "kt10000" if side == "BUY" else "kt10001"
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": symbol.zfill(6),
            "ord_qty": str(int(quantity)),
            "ord_uv": "0" if market_order else str(int(price)),
            "trde_tp": "3" if market_order else "0",
        }
        headers = self._auth.auth_headers({"api-id": api_id, "cont-yn": "N", "next-key": ""})
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if int(data.get("return_code", 0)) != 0:
                logger.error("❌ Kiwoom %s 주문 실패 (%s): %s", side, symbol, data.get("return_msg"))
                return False
            return True
        except Exception as exc:
            logger.error("❌ Kiwoom %s 주문 예외 (%s): %s", side, symbol, exc)
            return False

    def _mark_fill(self, symbol: str, quantity: int, price: float, side: str) -> None:
        state = self._read_state()
        holdings = state.setdefault("holdings", {})
        cash = self._safe_float(state.get("cash"))
        if side == "BUY":
            cost = float(quantity) * float(price)
            state["cash"] = cash - cost
            cur = holdings.get(symbol, {})
            old_qty = self._safe_int(cur.get("quantity"))
            old_amt = self._safe_float(cur.get("amount"))
            new_qty = old_qty + quantity
            new_amt = old_amt + cost
            holdings[symbol] = {
                "quantity": new_qty,
                "price": (new_amt / new_qty) if new_qty else 0.0,
                "amount": new_amt,
                "highest_price": max(self._safe_float(cur.get("highest_price")), float(price)),
            }
        else:
            cur = holdings.get(symbol, {})
            old_qty = self._safe_int(cur.get("quantity"))
            sell_qty = min(old_qty, int(quantity))
            state["cash"] = cash + float(sell_qty) * float(price)
            remain = old_qty - sell_qty
            if remain <= 0:
                holdings.pop(symbol, None)
            else:
                avg_price = self._safe_float(cur.get("price"))
                holdings[symbol] = {
                    "quantity": remain,
                    "price": avg_price,
                    "amount": avg_price * remain,
                    "highest_price": self._safe_float(cur.get("highest_price") or avg_price),
                }
        self._write_state(state)

    def place_buy_order(self, symbol: str, quantity: int, price: float, allow_price_chase: bool = False, market_order: bool = False) -> bool:
        if quantity <= 0:
            return False
        if not self._submit_order(symbol, quantity, price, "BUY", market_order=market_order):
            return False
        self._mark_fill(symbol, quantity, price, "BUY")
        return True

    def place_sell_order(self, symbol: str, quantity: int, price: float, market_order: bool = False) -> bool:
        if quantity <= 0:
            return False
        if not self._submit_order(symbol, quantity, price, "SELL", market_order=market_order):
            return False
        self._mark_fill(symbol, quantity, price, "SELL")
        return True

    def verify_domestic_fill(
        self,
        symbol: str,
        side: str,
        previous_qty: int,
        order_qty: int,
        retries: int | None = None,
        delay_sec: float | None = None,
    ) -> bool | str:
        balance = self.get_balance()
        holdings = balance.get("holdings", {})
        current_qty = int(holdings.get(symbol, {}).get("quantity", 0) or 0)
        if side.upper() == "BUY":
            return current_qty >= previous_qty + order_qty
        return current_qty <= max(0, previous_qty - order_qty)
