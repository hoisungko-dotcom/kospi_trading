"""키움 REST API 클라이언트 — 토큰 관리 + 차트/시세 조회."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env")

BASE = os.getenv("KIWOOM_BASE_URL", "https://api.kiwoom.com").rstrip("/")
logger = logging.getLogger(__name__)

_token: str = ""
_token_expires: datetime = datetime.min
_last_token_issue_monotonic: float = 0.0
_TOKEN_GUARD_SEC = 30.0
_TOKEN_CACHE_PATH = Path(__file__).parents[1] / "data" / "kiwoom_token_cache.json"


def _invalidate_token() -> None:
    global _token, _token_expires
    _token = ""
    _token_expires = datetime.min
    try:
        _TOKEN_CACHE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _load_cached_token() -> bool:
    global _token, _token_expires
    try:
        if not _TOKEN_CACHE_PATH.exists():
            return False
        payload = json.loads(_TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        token = str(payload.get("token") or "").strip()
        expires_raw = str(payload.get("expires_dt") or "").strip()
        if not token or not expires_raw:
            return False
        expires_dt = datetime.strptime(expires_raw[:14], "%Y%m%d%H%M%S")
        if datetime.now() >= expires_dt - timedelta(minutes=10):
            return False
        _token = token
        _token_expires = expires_dt
        logger.info("✅ 키움 캐시 토큰 재사용 (만료: %s)", _token_expires)
        return True
    except Exception:
        return False


def _save_cached_token(token: str, expires_dt: datetime) -> None:
    try:
        _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_CACHE_PATH.write_text(json.dumps({"token": token, "expires_dt": expires_dt.strftime("%Y%m%d%H%M%S")}), encoding="utf-8")
    except Exception:
        pass


def _guard_token_issue_interval() -> None:
    global _last_token_issue_monotonic
    elapsed = time.monotonic() - _last_token_issue_monotonic
    if _last_token_issue_monotonic > 0 and elapsed < _TOKEN_GUARD_SEC:
        wait_sec = _TOKEN_GUARD_SEC - elapsed
        logger.info("키움 토큰 재발급 간격 대기 %.1fs", wait_sec)
        time.sleep(wait_sec)


def _get_token() -> str:
    global _token, _token_expires, _last_token_issue_monotonic
    if _token and datetime.now() < _token_expires - timedelta(minutes=10):
        return _token
    if _load_cached_token():
        return _token

    _guard_token_issue_interval()
    last_error = None
    for delay_sec in (0.0, 1.0, 2.0, 5.0):
        if delay_sec > 0:
            logger.warning("키움 토큰 재시도 전 %.1fs 대기", delay_sec)
            time.sleep(delay_sec)
        _last_token_issue_monotonic = time.monotonic()
        r = requests.post(
            f"{BASE}/oauth2/token",
            json={
                "grant_type": "client_credentials",
                "appkey": os.environ["KIWOOM_APPKEY"],
                "secretkey": os.environ["KIWOOM_APPSECRET"],
            },
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=15,
        )
        if r.status_code == 429:
            last_error = RuntimeError("토큰 발급 429")
            logger.warning("키움 토큰 429 제한 감지")
            continue
        r.raise_for_status()
        d = r.json()
        if d.get("return_code") != 0:
            raise RuntimeError(f"토큰 발급 실패: {d.get('return_msg')}")

        _token = d["token"]
        _token_expires = datetime.strptime(d["expires_dt"], "%Y%m%d%H%M%S")
        _save_cached_token(_token, _token_expires)
        logger.info("✅ 키움 토큰 발급 (만료: %s)", _token_expires)
        return _token
    raise last_error or RuntimeError("토큰 발급 실패")


def _headers(api_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "Authorization": f"Bearer {_get_token()}",
        "api-id": api_id,
    }


def _post(api_id: str, path: str, payload: dict, *, timeout: int = 15) -> dict:
    res = requests.post(f"{BASE}{path}", json=payload, headers=_headers(api_id), timeout=timeout)
    res.raise_for_status()
    return res.json()


def get_min_chart(stk_cd: str, tic_scope: str = "1", cont_key: str = "", max_pages: int = 1) -> list[dict]:
    rows: list[dict] = []
    ck = cont_key
    for _ in range(max_pages):
        retry_after_token_refresh = False
        for _attempt in range(2):
            payload = {"stk_cd": stk_cd, "tic_scope": tic_scope, "qry_tp": "0", "upd_stkpc_tp": "1"}
            req_headers = _headers("ka10080")
            if ck:
                req_headers["cont-key"] = ck
                req_headers["cont-yn"] = "Y"
            r = requests.post(f"{BASE}/api/dostk/chart", json=payload, headers=req_headers, timeout=15)
            r.raise_for_status()
            d = r.json()
            if d.get("return_code") == 0:
                break
            msg = d.get("return_msg", "")
            if "8005" in msg or "Token이 유효하지 않습니다" in msg:
                _invalidate_token()
                _get_token()
                retry_after_token_refresh = True
                continue
            logger.warning("1분봉 오류 %s: %s", stk_cd, msg)
            d = None
            break
        else:
            d = None
        if not d:
            if retry_after_token_refresh:
                logger.warning("1분봉 재시도 실패 %s", stk_cd)
            break
        chunk = d.get("stk_min_pole_chart_qry", [])
        rows.extend(chunk)
        ck = r.headers.get("cont-key", "")
        if not ck or len(chunk) < 900:
            break
        time.sleep(0.12)
    return rows


def get_stock_list(market: str = "0", only_normal: bool = True) -> list[dict]:
    d = _post("ka10099", "/api/dostk/stkinfo", {"mrkt_tp": market})
    if d.get("return_code") != 0:
        logger.warning("종목 목록 오류: %s", d.get("return_msg"))
        return []
    rows = d.get("list", [])
    if only_normal:
        rows = [s for s in rows if s.get("kind") == "A"]
    return rows


def get_basic_price(stk_cd: str) -> dict:
    d = _post("ka10007", "/api/dostk/stkinfo", {"stk_cd": stk_cd})
    return d if isinstance(d, dict) else {}


def parse_candle(row: dict) -> dict:
    def _price(s: str) -> float:
        return float(str(s).lstrip("+-")) if s else 0.0

    return {
        "ts": row.get("cntr_tm", ""),
        "open": _price(row.get("open_pric", "0")),
        "high": _price(row.get("high_pric", "0")),
        "low": _price(row.get("low_pric", "0")),
        "close": _price(row.get("cur_prc", "0")),
        "volume": int(str(row.get("trde_qty", "0")).lstrip("+-") or 0),
    }
