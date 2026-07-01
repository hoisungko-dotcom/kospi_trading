"""
Kiwoom REST API data provider.

Data-only: current price + minute bars.
No order functions.
"""
from __future__ import annotations

import logging
import os
import time
import json
from typing import Any

import pandas as pd
import requests

from brokers.kiwoom.auth import KiwoomAuthClient

logger = logging.getLogger(__name__)


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _safe_price(val: Any, default: float = 0.0) -> float:
    return abs(_safe_float(val, default))


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(str(val).replace(",", ""))
    except (TypeError, ValueError):
        return default


class KiwoomDataProvider:
    _DEFAULT_BASE = "https://api.kiwoom.com"

    def __init__(self, auth: KiwoomAuthClient) -> None:
        self._auth = auth
        self._base = os.environ.get("KIWOOM_BASE_URL", self._DEFAULT_BASE).rstrip("/")

    @classmethod
    def from_env(cls) -> "KiwoomDataProvider":
        return cls(KiwoomAuthClient.from_env())

    def get_current_price(self, symbol: str) -> dict | None:
        url = self._base + "/api/dostk/stkinfo"
        headers = self._auth.auth_headers({
            "api-id": "ka10001",
            "cont-yn": "N",
            "next-key": "",
        })
        body = {"stk_cd": symbol.zfill(6)}
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            raw_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            logger.info("현재가 응답 키 (%s): %s", symbol, raw_keys)
            return {
                "symbol": symbol,
                "current": _safe_price(data.get("stck_prpr") or data.get("cur_prc") or data.get("current_price")),
                "open": _safe_price(data.get("stck_oppr") or data.get("open_pric") or data.get("open_prc") or data.get("open_price")),
                "high": _safe_price(data.get("stck_hgpr") or data.get("high_pric") or data.get("high_prc") or data.get("high_price")),
                "low": _safe_price(data.get("stck_lwpr") or data.get("low_pric") or data.get("low_prc") or data.get("low_price")),
                "volume": _safe_int(data.get("acml_vol") or data.get("trde_qty") or data.get("volume")),
                "raw": data,
            }
        except requests.HTTPError as exc:
            logger.warning("Kiwoom 현재가 HTTP 오류 (%s): %s — 응답: %s", symbol, exc, exc.response.text[:400] if exc.response else "")
            return None
        except Exception as exc:
            logger.warning("Kiwoom 현재가 실패 (%s): %s", symbol, exc)
            return None

    def get_current_prices(self, symbols: list[str]) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        for symbol in symbols:
            info = self.get_current_price(symbol)
            result[symbol] = info["current"] if info and info["current"] > 0 else None
            time.sleep(0.05)
        return result

    def get_minute_bars(self, symbol: str, interval: int = 1, lookback: int = 30) -> list[dict] | None:
        url = self._base + "/api/dostk/chart"
        headers = self._auth.auth_headers({
            "api-id": "ka10080",
            "cont-yn": "N",
            "next-key": "",
        })
        body = {"stk_cd": symbol.zfill(6), "tic_scope": str(interval), "upd_stkpc_tp": "1"}
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                logger.info("분봉 응답 최상위 키 (%s): %s", symbol, list(data.keys()))
            rows: list[dict] = data.get("stk_min_pole_chart_qry") or data.get("data") or data.get("output") or []
            if not rows and isinstance(data, list):
                rows = data
            if rows:
                logger.info("분봉 첫 행 키: %s", list(rows[0].keys()) if rows else [])
            bars: list[dict] = []
            for row in rows[:lookback]:
                bars.append({
                    "time": row.get("cntr_tm") or row.get("stck_cntg_hour") or row.get("time") or "",
                    "open": _safe_price(row.get("stck_oppr") or row.get("open_pric") or row.get("open_prc") or row.get("open")),
                    "high": _safe_price(row.get("stck_hgpr") or row.get("high_pric") or row.get("high_prc") or row.get("high")),
                    "low": _safe_price(row.get("stck_lwpr") or row.get("low_pric") or row.get("low_prc") or row.get("low")),
                    "close": _safe_price(row.get("stck_prpr") or row.get("cur_prc") or row.get("close")),
                    "volume": _safe_int(row.get("cntg_vol") or row.get("trde_qty") or row.get("volume")),
                })
            return bars if bars else None
        except requests.HTTPError as exc:
            logger.warning("Kiwoom 분봉 HTTP 오류 (%s): %s — 응답: %s", symbol, exc, exc.response.text[:400] if exc.response else "")
            return None

    def get_minute_bars_df(self, symbol: str, interval: int = 1, lookback: int = 30) -> pd.DataFrame | None:
        bars = self.get_minute_bars(symbol, interval=interval, lookback=lookback)
        if not bars:
            return None
        df = pd.DataFrame(bars)
        if df.empty:
            return None
        if "time" in df.columns:
            df["time"] = df["time"].astype(str)
        return df

    def get_tick_bars(self, symbol: str, tick_scope: int = 1, lookback: int = 60) -> list[dict] | None:
        url = self._base + "/api/dostk/chart"
        headers = self._auth.auth_headers({
            "api-id": "ka10079",
            "cont-yn": "N",
            "next-key": "",
        })
        body = {"stk_cd": symbol.zfill(6), "tic_scope": str(tick_scope), "upd_stkpc_tp": "1"}
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            rows: list[dict] = data.get("stk_tic_chart_qry") or data.get("data") or data.get("output") or []
            if not rows and isinstance(data, list):
                rows = data
            bars: list[dict] = []
            for row in rows[:lookback]:
                bars.append({
                    "time": row.get("cntr_tm") or row.get("time") or "",
                    "open": _safe_price(row.get("open_pric") or row.get("stck_oppr") or row.get("open")),
                    "high": _safe_price(row.get("high_pric") or row.get("stck_hgpr") or row.get("high")),
                    "low": _safe_price(row.get("low_pric") or row.get("stck_lwpr") or row.get("low")),
                    "close": _safe_price(row.get("cur_prc") or row.get("stck_prpr") or row.get("close")),
                    "volume": _safe_int(row.get("trde_qty") or row.get("cntg_vol") or row.get("volume")),
                })
            return bars if bars else None
        except requests.HTTPError as exc:
            logger.warning("Kiwoom 틱 HTTP 오류 (%s): %s — 응답: %s", symbol, exc, exc.response.text[:400] if exc.response else "")
            return None
        except Exception as exc:
            logger.warning("Kiwoom 틱 실패 (%s): %s", symbol, exc)
            return None

    def get_tick_bars_df(self, symbol: str, tick_scope: int = 1, lookback: int = 60) -> pd.DataFrame | None:
        bars = self.get_tick_bars(symbol, tick_scope=tick_scope, lookback=lookback)
        if not bars:
            return None
        df = pd.DataFrame(bars)
        if df.empty:
            return None
        if "time" in df.columns:
            df["time"] = df["time"].astype(str)
        return df

    def get_daily_bars(self, symbol: str, lookback: int = 300) -> pd.DataFrame | None:
        url = self._base + "/api/dostk/chart"
        headers = self._auth.auth_headers({
            "api-id": "ka10081",
            "cont-yn": "N",
            "next-key": "",
        })
        body = {"stk_cd": symbol.zfill(6), "base_dt": time.strftime("%Y%m%d"), "upd_stkpc_tp": "1"}
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            rows: list[dict] = data.get("stk_dt_pole_chart_qry") or data.get("stk_day_pole_chart_qry") or data.get("data") or data.get("output") or []
            if not rows and isinstance(data, list):
                rows = data
            parsed = []
            for row in rows[:lookback]:
                parsed.append({
                    "date": row.get("dt") or row.get("stck_bsop_date") or row.get("date") or "",
                    "open": _safe_price(row.get("open_pric") or row.get("stck_oprc") or row.get("open")),
                    "high": _safe_price(row.get("high_pric") or row.get("stck_hgpr") or row.get("high")),
                    "low": _safe_price(row.get("low_pric") or row.get("stck_lwpr") or row.get("low")),
                    "close": _safe_price(row.get("cur_prc") or row.get("close_pric") or row.get("stck_clpr") or row.get("stck_prpr") or row.get("close")),
                    "volume": _safe_int(row.get("trde_qty") or row.get("acml_vol") or row.get("volume")),
                })
            df = pd.DataFrame(parsed)
            if df.empty:
                return None
            return df.sort_values("date").tail(lookback)
        except requests.HTTPError as exc:
            logger.warning("Kiwoom 일봉 HTTP 오류 (%s): %s — 응답: %s", symbol, exc, exc.response.text[:400] if exc.response else "")
            return None
        except Exception as exc:
            logger.warning("Kiwoom 일봉 실패 (%s): %s", symbol, exc)
            return None

    def _post_rankinfo(
        self,
        api_id: str,
        body: dict,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> tuple[dict | None, str, str]:
        url = self._base + "/api/dostk/rkinfo"
        for attempt in range(2):
            headers = self._auth.auth_headers({
                "api-id": api_id,
                "cont-yn": cont_yn,
                "next-key": next_key,
            })
            try:
                resp = requests.post(url, json=body, headers=headers, timeout=15)
                if resp.status_code == 429 and attempt == 0:
                    time.sleep(1.0)
                    continue
                resp.raise_for_status()
                return (
                    resp.json(),
                    str(resp.headers.get("cont-yn", "N") or "N"),
                    str(resp.headers.get("next-key", "") or ""),
                )
            except requests.HTTPError as exc:
                logger.warning(
                    "Kiwoom 순위 HTTP 오류 (%s): %s — 응답: %s",
                    api_id,
                    exc,
                    exc.response.text[:400] if exc.response else "",
                )
            except Exception as exc:
                logger.warning("Kiwoom 순위 실패 (%s): %s", api_id, exc)
        return None, "N", ""

    def _extract_rank_rows(self, data: dict, preferred_keys: list[str]) -> list[dict]:
        for key in preferred_keys:
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        return []

    def get_trading_value_rank(self, market: str = "001", include_managed: bool = True) -> list[dict]:
        body = {
            "mrkt_tp": market,
            "mang_stk_incls": "1" if include_managed else "0",
            "stex_tp": os.getenv("KIWOOM_RANK_STEX_TP", "1"),
        }
        all_rows: list[dict] = []
        cont_yn = "N"
        next_key = ""
        max_pages = int(os.getenv("KIWOOM_RANK_MAX_PAGES", "3") or 3)
        for _ in range(max_pages):
            data, cont_yn, next_key = self._post_rankinfo("ka10032", body, cont_yn=cont_yn, next_key=next_key)
            if not data:
                break
            rows = self._extract_rank_rows(data, ["trde_prica_upper"])
            all_rows.extend(rows)
            if cont_yn != "Y" or not next_key:
                break
        return all_rows

    def get_investor_net_buy_rank(self, market: str = "001") -> list[dict]:
        raw_body = os.getenv("KIWOOM_NET_BUY_RANK_BODY_JSON", "").strip()
        if raw_body:
            try:
                body = json.loads(raw_body)
            except Exception as exc:
                logger.warning("KIWOOM_NET_BUY_RANK_BODY_JSON 파싱 실패: %s", exc)
                body = {}
        else:
            body = {
                "mrkt_tp": market,
                "sort_base": os.getenv("KIWOOM_NET_BUY_SORT_BASE", "1"),
                "stk_cnd": os.getenv("KIWOOM_NET_BUY_STK_CND", "1"),
                "trde_qty_cnd": os.getenv("KIWOOM_NET_BUY_QTY_CND", "00000"),
                "crd_cnd": os.getenv("KIWOOM_NET_BUY_CRD_CND", "0"),
                "trde_prica": os.getenv("KIWOOM_NET_BUY_TRDE_PRICA", "0"),
                "qry_dt_tp": os.getenv("KIWOOM_NET_BUY_QRY_DT_TP", "0"),
                "trde_tp": os.getenv("KIWOOM_NET_BUY_TRADE_TYPE", "0"),
                "amt_qty_tp": os.getenv("KIWOOM_NET_BUY_AMOUNT_TYPE", "1"),
                "stex_tp": os.getenv("KIWOOM_RANK_STEX_TP", "1"),
            }

        api_id = os.getenv("KIWOOM_NET_BUY_RANK_API_ID", "ka90009")
        data, _cont_yn, _next_key = self._post_rankinfo(api_id, body)
        if not data:
            return []
        rows = self._extract_rank_rows(
            data,
            ["frgnr_orgn_trde_upper", "sec_trde_upper", "frgn_orgn_trde_upper", "data", "output"],
        )
        if rows:
            return rows
        return []

    def speed_test(self, symbols: list[str]) -> dict:
        times: list[float] = []
        successes: list[str] = []
        failures: list[str] = []
        for sym in symbols:
            t0 = time.perf_counter()
            info = self.get_current_price(sym)
            elapsed = time.perf_counter() - t0
            if info and info["current"] > 0:
                successes.append(sym)
                times.append(elapsed)
            else:
                failures.append(sym)
        avg = (sum(times) / len(times)) if times else 0.0
        return {"successes": successes, "failures": failures, "avg_sec": avg, "count": len(symbols)}
