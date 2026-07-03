from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

from collector.kiwoom_client import BASE as DEFAULT_BASE
from collector.kiwoom_client import get_basic_price

logger = logging.getLogger(__name__)

_TOKEN_TTL_SEC = 60 * 60 * 23
_STATE_PATH = Path(__file__).parents[1] / "data" / "kiwoom_mock_account_state.json"


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip() or default)
    except Exception:
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "").strip() or default))
    except Exception:
        return default


class KiwoomMockDomesticBroker:
    def __init__(self) -> None:
        self.base_url = os.getenv("KIWOOM_BASE_URL", DEFAULT_BASE).rstrip("/")
        self.ws_url = os.getenv("KIWOOM_WS_URL", "wss://mockapi.kiwoom.com:10000/api/dostk/websocket")
        self.app_key = os.getenv("KIWOOM_APPKEY", "")
        self.app_secret = os.getenv("KIWOOM_APPSECRET", "")
        self.account_no = os.getenv("KIWOOM_ACCOUNT_NO", "").replace("-", "").strip()
        self.market = os.getenv("KIWOOM_MARKET", "KRX").strip() or "KRX"
        self.initial_cash = _to_float(os.getenv("KIWOOM_MOCK_INITIAL_CASH", "10000000"), 10_000_000.0)
        if not self.app_key or not self.app_secret or not self.account_no:
            raise RuntimeError("Kiwoom mock credentials missing")
        self._token: str | None = None
        self._token_at = 0.0
        self.last_reject_message = ""
        self.last_error_message = ""
        self._state_path = _STATE_PATH
        self._ensure_state_file()
        self._issue_token()

    def _ensure_state_file(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        if self._state_path.exists():
            return
        payload = {
            "cash": self.initial_cash,
            "holdings": {},
            "updated_at": time.time(),
        }
        self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_state(self) -> dict:
        self._ensure_state_file()
        return json.loads(self._state_path.read_text(encoding="utf-8"))

    def _save_state(self, payload: dict) -> None:
        payload["updated_at"] = time.time()
        self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def reset_state(self, *, cash: float | None = None) -> None:
        payload = {
            "cash": float(self.initial_cash if cash is None else cash),
            "holdings": {},
            "updated_at": time.time(),
        }
        self._save_state(payload)

    def _issue_token(self) -> None:
        res = requests.post(
            f"{self.base_url}/oauth2/token",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "secretkey": self.app_secret,
            },
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        if data.get("return_code") != 0:
            raise RuntimeError(f"Kiwoom token issue failed: {data.get('return_msg')}")
        self._token = data.get("token")
        self._token_at = time.time()

    def _ensure_token(self) -> None:
        if not self._token or (time.time() - self._token_at) > _TOKEN_TTL_SEC:
            self._issue_token()

    def _headers(self, api_id: str) -> dict[str, str]:
        self._ensure_token()
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": f"Bearer {self._token}",
            "api-id": api_id,
        }

    def _request(self, api_id: str, path: str, payload: dict) -> dict | None:
        self.last_reject_message = ""
        self.last_error_message = ""
        try:
            res = requests.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=self._headers(api_id),
                timeout=20,
            )
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict) and data.get("return_code", 0) not in (0, "0", None):
                self.last_reject_message = str(data.get("return_msg") or "unknown reject")
                return None
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self.last_error_message = str(exc)
            logger.warning("Kiwoom mock request failed %s: %s", api_id, exc)
            return None

    def _current_price(self, symbol: str) -> float:
        data = get_basic_price(symbol)
        if isinstance(data, dict):
            body = data.get("data")
            if isinstance(body, list):
                body = body[0] if body else {}
            if not isinstance(body, dict):
                body = data
            return _to_float(
                body.get("cur_prc") or body.get("price") or body.get("close") or body.get("curPrice") or 0
            )
        return 0.0

    def get_balance(self) -> dict:
        state = self._load_state()
        stock_value = 0.0
        holdings_list = []
        for code, item in (state.get("holdings") or {}).items():
            qty = _to_int(item.get("qty"), 0)
            if qty <= 0:
                continue
            current_price = self._current_price(code) or _to_float(item.get("entry_price"), 0.0)
            stock_value += current_price * qty
            holdings_list.append((code, qty, current_price))
        cash = _to_float(state.get("cash"), self.initial_cash)
        return {
            "cash": cash,
            "stock_value": stock_value,
            "total_assets": cash + stock_value,
        }

    def get_holdings(self) -> list[dict]:
        state = self._load_state()
        holdings = []
        for code, item in (state.get("holdings") or {}).items():
            qty = _to_int(item.get("qty"), 0)
            if qty <= 0:
                continue
            current_price = self._current_price(code) or _to_float(item.get("entry_price"), 0.0)
            holdings.append(
                {
                    "code": code,
                    "name": item.get("name", code),
                    "qty": qty,
                    "entry_price": _to_float(item.get("entry_price"), 0.0),
                    "current_price": current_price,
                }
            )
        return holdings

    def inquire_buyable_qty(self, symbol: str, *, market_order: bool = True, price: float = 0.0) -> int:
        state = self._load_state()
        effective_price = price if price > 0 else self._current_price(symbol)
        if effective_price <= 0:
            return 0
        return int(_to_float(state.get("cash"), 0.0) // effective_price)

    def buy(self, symbol: str, qty: int) -> dict | None:
        if qty <= 0:
            self.last_reject_message = "qty_zero"
            return None
        price = self._current_price(symbol)
        if price <= 0:
            self.last_reject_message = "price_unavailable"
            return None
        state = self._load_state()
        cost = price * qty
        cash = _to_float(state.get("cash"), 0.0)
        if cost > cash:
            self.last_reject_message = f"insufficient_cash:{int(cash)}<{int(cost)}"
            return None
        response = self._request(
            "kt10000",
            "/api/dostk/ordr",
            {
                "dmst_stex_tp": self.market,
                "stk_cd": symbol,
                "ord_qty": str(qty),
                "ord_uv": "",
                "trde_tp": "3",
            },
        )
        if not response:
            return None
        holding = (state.get("holdings") or {}).get(symbol, {})
        prev_qty = _to_int(holding.get("qty"), 0)
        prev_entry = _to_float(holding.get("entry_price"), 0.0)
        new_qty = prev_qty + qty
        avg_price = ((prev_entry * prev_qty) + cost) / new_qty if new_qty > 0 else price
        (state.setdefault("holdings", {}))[symbol] = {
            "name": holding.get("name", symbol),
            "qty": new_qty,
            "entry_price": avg_price,
        }
        state["cash"] = cash - cost
        self._save_state(state)
        return response

    def sell(self, symbol: str, qty: int) -> dict | None:
        state = self._load_state()
        holding = (state.get("holdings") or {}).get(symbol)
        if not holding:
            self.last_reject_message = "not_holding"
            return None
        current_qty = _to_int(holding.get("qty"), 0)
        if qty <= 0 or qty > current_qty:
            self.last_reject_message = f"invalid_qty:{qty}/{current_qty}"
            return None
        price = self._current_price(symbol)
        if price <= 0:
            self.last_reject_message = "price_unavailable"
            return None
        response = self._request(
            "kt10001",
            "/api/dostk/ordr",
            {
                "dmst_stex_tp": self.market,
                "stk_cd": symbol,
                "ord_qty": str(qty),
                "ord_uv": "",
                "trde_tp": "3",
            },
        )
        if not response:
            return None
        remain = current_qty - qty
        if remain > 0:
            holding["qty"] = remain
        else:
            state.get("holdings", {}).pop(symbol, None)
        state["cash"] = _to_float(state.get("cash"), 0.0) + (price * qty)
        self._save_state(state)
        return response

    def sell_and_confirm(self, symbol: str, qty: int) -> tuple[dict | None, bool]:
        order = self.sell(symbol, qty)
        return order, bool(order)
