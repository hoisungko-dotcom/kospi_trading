import json
import logging
import time
import xml.etree.ElementTree as ET
from urllib import request

logger = logging.getLogger(__name__)

_CACHE: dict = {}
_TTL = {'vix': 1800, 'fear_greed': 3600}


def _cached(key: str, fetch_fn):
    now = time.time()
    if key in _CACHE:
        val, ts = _CACHE[key]
        if now - ts < _TTL[key]:
            return val
    try:
        val = fetch_fn()
        if val is not None:
            _CACHE[key] = (val, now)
        return val
    except Exception as e:
        logger.debug(f"[MarketMonitor] {key} 조회 실패: {e}")
        return _CACHE.get(key, (None, 0))[0]


def get_vix() -> float | None:
    def _fetch():
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="2d")
        if not hist.empty:
            return round(float(hist['Close'].iloc[-1]), 2)
        return None
    return _cached('vix', _fetch)


def get_fear_greed() -> dict | None:
    def _fetch():
        req = request.Request(
            "https://api.alternative.me/fng/?limit=1",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with request.urlopen(req, timeout=8) as resp:
            fng = json.loads(resp.read())['data'][0]
            return {'value': int(fng['value']), 'label': fng['value_classification']}
    return _cached('fear_greed', _fetch)




def get_summary() -> dict:
    return {'vix': get_vix(), 'fear_greed': get_fear_greed()}


def format_log(summary: dict) -> str:
    parts = []
    vix = summary.get('vix')
    if vix is not None:
        level = "⚠️ 공포" if vix > 30 else ("😐 중립" if vix > 20 else "😊 안정")
        parts.append(f"VIX {vix:.1f} ({level})")
    fg = summary.get('fear_greed')
    if fg:
        val = fg['value']
        emoji = "😱" if val < 25 else ("😨" if val < 45 else ("😐" if val < 55 else ("😊" if val < 75 else "🤑")))
        parts.append(f"공포탐욕 {val}{emoji} {fg['label']}")
    return " | ".join(parts)
