from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / '.env')
load_dotenv(Path(__file__).parents[1] / '.env.ai_overrides', override=True)

logger = logging.getLogger(__name__)

BASE = os.getenv('KIS_REAL_BASE_URL', 'https://openapi.koreainvestment.com:9443').rstrip('/')
APPKEY = os.getenv('KIS_REAL_APPKEY', '').strip()
APPSECRET = os.getenv('KIS_REAL_APPSECRET', '').strip()
ENABLED = bool(APPKEY and APPSECRET)

_token = ''
_token_expires = datetime.min
_TOKEN_CACHE_PATH = Path(__file__).parents[1] / 'data' / 'kis_real_token_cache.json'


def _invalidate_token() -> None:
    global _token, _token_expires
    _token = ''
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
        payload = json.loads(_TOKEN_CACHE_PATH.read_text(encoding='utf-8'))
        token = str(payload.get('token') or '').strip()
        expires_raw = str(payload.get('expires_dt') or '').strip()
        if not token or not expires_raw:
            return False
        expires_dt = datetime.strptime(expires_raw[:14], '%Y%m%d%H%M%S')
        if datetime.now() >= expires_dt - timedelta(minutes=10):
            return False
        _token = token
        _token_expires = expires_dt
        return True
    except Exception:
        return False


def _save_cached_token(token: str, expires_dt: datetime) -> None:
    try:
        _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_CACHE_PATH.write_text(json.dumps({'token': token, 'expires_dt': expires_dt.strftime('%Y%m%d%H%M%S')}), encoding='utf-8')
    except Exception:
        pass


def _get_token() -> str:
    global _token, _token_expires
    if not ENABLED:
        raise RuntimeError('KIS real credentials missing')
    if _token and datetime.now() < _token_expires - timedelta(minutes=10):
        return _token
    if _load_cached_token():
        return _token

    res = requests.post(
        f'{BASE}/oauth2/tokenP',
        json={
            'grant_type': 'client_credentials',
            'appkey': APPKEY,
            'appsecret': APPSECRET,
        },
        timeout=15,
    )
    res.raise_for_status()
    data = res.json()
    token = str(data.get('access_token') or '').strip()
    if not token:
        raise RuntimeError(f'KIS real token missing: {data}')
    expires_sec = int(data.get('expires_in') or 86400)
    _token = token
    _token_expires = datetime.now() + timedelta(seconds=expires_sec)
    _save_cached_token(_token, _token_expires)
    return _token


def _headers(tr_id: str) -> dict[str, str]:
    return {
        'content-type': 'application/json; charset=utf-8',
        'authorization': f'Bearer {_get_token()}',
        'appkey': APPKEY,
        'appsecret': APPSECRET,
        'tr_id': tr_id,
        'custtype': 'P',
    }


def _get(path: str, tr_id: str, params: dict[str, str], *, timeout: int = 15) -> dict:
    res = requests.get(f'{BASE}{path}', headers=_headers(tr_id), params=params, timeout=timeout)
    res.raise_for_status()
    data = res.json()
    rt_cd = str(data.get('rt_cd', ''))
    msg = data.get('msg1', '')
    if rt_cd and rt_cd != '0':
        if '토큰' in msg or 'TOKEN' in msg.upper():
            _invalidate_token()
        raise RuntimeError(f'KIS API error [{data.get(msg_cd, )}] {msg}')
    return data if isinstance(data, dict) else {}


def kis_enabled() -> bool:
    return ENABLED


def get_min_chart(stk_cd: str, tic_scope: str = '1', cont_key: str = '', max_pages: int = 1) -> list[dict]:
    del tic_scope, cont_key
    rows: list[dict] = []
    cursor = datetime.now().strftime('%H%M%S')
    for _ in range(max_pages):
        data = _get(
            '/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice',
            'FHKST03010200',
            {
                'FID_ETC_CLS_CODE': '',
                'FID_COND_MRKT_DIV_CODE': 'J',
                'FID_INPUT_ISCD': stk_cd,
                'FID_INPUT_HOUR_1': cursor,
                'FID_PW_DATA_INCU_YN': 'Y',
            },
            timeout=20,
        )
        chunk = data.get('output2') or []
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(chunk)
        times = [str(item.get('stck_cntg_hour') or '').strip() for item in chunk if isinstance(item, dict)]
        times = [t for t in times if len(t) == 6]
        if len(chunk) < 30 or not times:
            break
        next_cursor = min(times)
        if next_cursor <= '090000' or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(0.12)
    return rows


def parse_candle(row: dict) -> dict:
    def _price(value: str | int | float) -> float:
        text = str(value or '').replace(',', '').strip()
        return float(text.lstrip('+-')) if text else 0.0

    return {
        'ts': row.get('stck_bsop_date', datetime.now().strftime('%Y%m%d')) + str(row.get('stck_cntg_hour') or ''),
        'open': _price(row.get('stck_oprc')),
        'high': _price(row.get('stck_hgpr')),
        'low': _price(row.get('stck_lwpr')),
        'close': _price(row.get('stck_prpr')),
        'volume': int(float(str(row.get('cntg_vol') or '0').replace(',', '').strip() or 0)),
    }


def get_basic_price(stk_cd: str) -> dict:
    return _get(
        '/uapi/domestic-stock/v1/quotations/inquire-price',
        'FHKST01010100',
        {
            'FID_COND_MRKT_DIV_CODE': 'J',
            'FID_INPUT_ISCD': stk_cd,
        },
        timeout=10,
    )


def get_daily_candles(stk_cd: str, *, days: int = 60) -> list[dict]:
    today = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=max(days * 2, 120))).strftime('%Y%m%d')
    data = _get(
        '/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice',
        'FHKST03010100',
        {
            'FID_COND_MRKT_DIV_CODE': 'J',
            'FID_INPUT_ISCD': stk_cd,
            'FID_INPUT_DATE_1': start,
            'FID_INPUT_DATE_2': today,
            'FID_PERIOD_DIV_CODE': 'D',
            'FID_ORG_ADJ_PRC': '1',
        },
        timeout=20,
    )
    output = data.get('output2') or []
    candles: list[dict] = []
    for row in reversed(output if isinstance(output, list) else []):
        def p(value: str | int | float) -> float:
            text = str(value or '').replace(',', '').strip()
            return float(text.lstrip('+-')) if text else 0.0
        candles.append({
            'dt': str(row.get('stck_bsop_date') or ''),
            'open': p(row.get('stck_oprc')),
            'high': p(row.get('stck_hgpr')),
            'low': p(row.get('stck_lwpr')),
            'close': p(row.get('stck_clpr') or row.get('stck_prpr')),
        })
    return [c for c in candles if c['close'] > 0][-days:]


def get_volume_rank(*, market: str = "0001") -> list[dict]:
    data = _get(
        '/uapi/domestic-stock/v1/quotations/volume-rank',
        'FHPST01710000',
        {
            'FID_COND_MRKT_DIV_CODE': 'J',
            'FID_COND_SCR_DIV_CODE': '20171',
            'FID_INPUT_ISCD': market,
            'FID_DIV_CLS_CODE': '0',
            'FID_BLNG_CLS_CODE': '0',
            'FID_TRGT_CLS_CODE': '111111111',
            'FID_TRGT_EXLS_CLS_CODE': '000000',
            'FID_INPUT_PRICE_1': '0',
            'FID_INPUT_PRICE_2': '0',
            'FID_VOL_CNT': '0',
            'FID_INPUT_DATE_1': '0',
        },
        timeout=30,
    )
    return list(data.get('output') or []) if data else []


def get_volume_power(*, market: str = "0001") -> list[dict]:
    data = _get(
        '/uapi/domestic-stock/v1/ranking/volume-power',
        'FHPST01680000',
        {
            'fid_trgt_exls_cls_code': '0',
            'fid_cond_mrkt_div_code': 'J',
            'fid_cond_scr_div_code': '20168',
            'fid_input_iscd': market,
            'fid_div_cls_code': '0',
            'fid_input_price_1': '0',
            'fid_input_price_2': '0',
            'fid_vol_cnt': '0',
            'fid_trgt_cls_code': '0',
        },
        timeout=30,
    )
    return list(data.get('output') or []) if data else []


def get_foreign_institution_total(*, market: str = "0001") -> list[dict]:
    data = _get(
        '/uapi/domestic-stock/v1/quotations/foreign-institution-total',
        'FHPTJ04400000',
        {
            'FID_COND_MRKT_DIV_CODE': 'V',
            'FID_COND_SCR_DIV_CODE': '16449',
            'FID_INPUT_ISCD': market,
            'FID_DIV_CLS_CODE': '0',
            'FID_RANK_SORT_CLS_CODE': '0',
            'FID_ETC_CLS_CODE': '0',
        },
        timeout=30,
    )
    return list(data.get('output') or []) if data else []
