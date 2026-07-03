#!/usr/bin/env python3
"""daily_runner.py 패치: Morning Surge Hunter 추가"""

path = '/home/ubuntu/kospi_box_bot/realtime/daily_runner.py'
with open(path) as f:
    src = f.read()

# ─── 패치 1: 서지 전역변수 (_RECENT_ENTRY_TIMES 바로 뒤) ─────────────────────
old1 = '_RECENT_ENTRY_TIMES: list[datetime] = []'
new1 = '_RECENT_ENTRY_TIMES: list[datetime] = []\n\n# Morning Surge Tracker\n_morning_surge_list: dict[str, dict] = {}\nSURGE_OBS_END_HHMM   = os.getenv("BOX_BOT_SURGE_OBS_END", "0935").strip() or "0935"\nSURGE_MIN_CHANGE_PCT  = float(os.getenv("BOX_BOT_SURGE_MIN_CHANGE_PCT", "2.0") or 2.0)\nSURGE_MIN_OPEN_RATIO  = float(os.getenv("BOX_BOT_SURGE_MIN_OPEN_RATIO", "1.5") or 1.5)'

assert old1 in src, f'FAIL: old1 not found'
src = src.replace(old1, new1, 1)

# ─── 패치 2: 두 함수 추가 (run_scan_loop 바로 앞) ─────────────────────────────
surge_funcs = '''
def _try_register_surge(stk_cd: str, name: str, candles: list) -> None:
    """관찰 구간에서 서지 후보 등록."""
    global _morning_surge_list
    if stk_cd in _morning_surge_list or len(candles) < 3:
        return
    latest = candles[-1]
    today = latest.ts[:8]
    day_candles = [c for c in candles if c.ts[:8] == today]
    if len(day_candles) < 2:
        return
    open_price = day_candles[0].open or day_candles[0].close
    if open_price <= 0:
        return
    change_pct = (latest.close - open_price) / open_price * 100
    if change_pct < SURGE_MIN_CHANGE_PCT:
        return
    avg_vol = sum(c.volume for c in day_candles[:-1]) / max(len(day_candles) - 1, 1)
    if avg_vol > 0 and latest.volume < avg_vol * SURGE_MIN_OPEN_RATIO:
        return
    _morning_surge_list[stk_cd] = {
        "name": name,
        "change_pct": round(change_pct, 2),
        "detected_at": latest.ts[8:12],
    }
    logger.info("Surge detected: %s(%s) +%.1f%% @ %s", name, stk_cd, change_pct, latest.ts[8:12])


def _prepend_surge_stocks(stocks: list[dict]) -> list[dict]:
    """서지 종목을 스캔 최우선 배치. 유니버스에 없으면 추가."""
    if not _morning_surge_list:
        return stocks
    existing = {s.get("code", "") for s in stocks}
    for code, info in _morning_surge_list.items():
        if code not in existing:
            stocks.append({"code": code, "name": info["name"]})
            existing.add(code)
    surge_codes = set(_morning_surge_list.keys())
    reordered = (
        [s for s in stocks if s.get("code") in surge_codes]
        + [s for s in stocks if s.get("code") not in surge_codes]
    )
    if surge_codes:
        logger.debug(
            "Surge priority %d stocks: %s",
            len(surge_codes),
            ", ".join(_morning_surge_list[c]["name"] for c in list(surge_codes)[:5]),
        )
    return reordered

'''

old2 = 'def run_scan_loop('
new2 = surge_funcs + 'def run_scan_loop('
assert old2 in src, 'FAIL: old2 not found'
src = src.replace(old2, new2, 1)

# ─── 패치 3: run_scan_loop 내부 — 캔들 파싱 직후 서지 감지 삽입 ─────────────
old3 = '''        # 최신순 → 시간순, 최근 봉
        candles = [Candle(**parse_candle(r)) for r in reversed(rows)]
        latest  = candles[-1]
        latest_prices[stk_cd] = latest.close

        # 기존 포지션 tick → 청산 판단'''
new3 = '''        # 최신순 → 시간순, 최근 봉
        candles = [Candle(**parse_candle(r)) for r in reversed(rows)]
        latest  = candles[-1]
        latest_prices[stk_cd] = latest.close

        # 관찰 구간: 서지 후보 등록 (매수 없음)
        _cur_hhmm = datetime.now(KST).strftime("%H%M")
        if _cur_hhmm < SURGE_OBS_END_HHMM:
            _try_register_surge(stk_cd, name, candles)

        # 기존 포지션 tick → 청산 판단'''
assert old3 in src, 'FAIL: old3 not found'
src = src.replace(old3, new3, 1)

# ─── 패치 4: 두 호출 지점에 _prepend_surge_stocks 래핑 ───────────────────────
old4a = 'stocks   = _attach_holding_stocks(_load_stocks(args.top, broker), engine)'
new4a = 'stocks   = _prepend_surge_stocks(_attach_holding_stocks(_load_stocks(args.top, broker), engine))'
assert old4a in src, 'FAIL: old4a not found'
src = src.replace(old4a, new4a, 1)

old4b = '                stocks = _attach_holding_stocks(_load_stocks(args.top, broker), engine)'
new4b = '                stocks = _prepend_surge_stocks(_attach_holding_stocks(_load_stocks(args.top, broker), engine))'
assert old4b in src, 'FAIL: old4b not found'
src = src.replace(old4b, new4b, 1)

with open(path, 'w') as f:
    f.write(src)

print('daily_runner.py 패치 완료 (4개 변경)')
