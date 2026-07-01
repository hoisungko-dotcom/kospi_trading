#!/usr/bin/env python3
"""
Kiwoom REST API 데이터 공급자 속도/정확도 비교 테스트.

실행:
  cd /home/ubuntu/kospi_trading_system
  python3 scripts/test_kiwoom_data_provider.py

출력:
  - 키움 토큰 발급 결과
  - 키움 현재가 조회 결과
  - KIS 현재가 조회 결과
  - 가격 괴리율
  - 분봉 조회 속도
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# ── sys.path setup ───────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kiwoom_test")

# ── Test universe ─────────────────────────────────────────────────────────────
TEST_SYMBOLS = [
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "035420",  # NAVER
    "005380",  # 현대차
    "035720",  # 카카오
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def _bar(label: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print('─' * 60)


def _fmt_price(price) -> str:
    if price is None or price == 0:
        return "조회실패"
    return f"{int(price):,}원"


def _fmt_ms(ms) -> str:
    if ms is None:
        return "n/a"
    return f"{ms:.0f}ms"


# ── Step 1: Kiwoom token ──────────────────────────────────────────────────────

def test_token():
    _bar("1. 키움 토큰 발급 테스트")
    try:
        from brokers.kiwoom.auth import KiwoomAuthClient
        auth = KiwoomAuthClient.from_env()
        t0 = time.perf_counter()
        auth.token()
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"  ✅ 토큰 발급 성공 ({elapsed:.0f}ms) — 만료: {auth._expires_at}")
        return auth
    except Exception as exc:
        print(f"  ❌ 토큰 발급 실패: {exc}")
        return None


# ── Step 2: Kiwoom current price ──────────────────────────────────────────────

def test_kiwoom_prices(auth) -> dict[str, float | None]:
    _bar("2. 키움 현재가 조회")
    from brokers.kiwoom.data_provider import KiwoomDataProvider
    provider = KiwoomDataProvider(auth)

    kiwoom_prices: dict[str, float | None] = {}
    kiwoom_times: dict[str, float] = {}

    for sym in TEST_SYMBOLS:
        t0 = time.perf_counter()
        info = provider.get_current_price(sym)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        kiwoom_times[sym] = elapsed_ms

        price = info["current"] if info and info["current"] > 0 else None
        kiwoom_prices[sym] = price
        status = "✅" if price else "❌"
        print(f"  {status} {sym}  {_fmt_price(price):>12}  ({elapsed_ms:.0f}ms)")
        if info and info.get("raw"):
            raw_keys = list(info["raw"].keys())[:5]
            print(f"       raw 응답 키: {raw_keys}")

    return kiwoom_prices


# ── Step 3: KIS current price ─────────────────────────────────────────────────

def test_kis_prices() -> dict[str, float | None]:
    _bar("3. KIS 현재가 조회")
    kis_prices: dict[str, float | None] = {}

    try:
        from brokers.kis.client import KISClientKospi
        client = KISClientKospi()

        t0 = time.perf_counter()
        try:
            prices = client.get_current_prices(TEST_SYMBOLS)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            for sym in TEST_SYMBOLS:
                price = prices.get(sym)
                kis_prices[sym] = float(price) if price else None
                status = "✅" if price else "❌"
                print(f"  {status} {sym}  {_fmt_price(kis_prices[sym]):>12}")
            print(f"  (전체 {elapsed_ms:.0f}ms)")
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning("KIS 현재가 실패: %s", exc)
            for sym in TEST_SYMBOLS:
                kis_prices[sym] = None
            print(f"  ❌ KIS 현재가 조회 실패 ({elapsed_ms:.0f}ms): {exc}")

    except ImportError as exc:
        print(f"  ⚠️  KIS 클라이언트 임포트 실패: {exc}")
        print("     KIS 비교 건너뜀.")

    return kis_prices


# ── Step 4: Price divergence ──────────────────────────────────────────────────

def compare_prices(kiwoom: dict, kis: dict) -> None:
    _bar("4. 가격 괴리율 비교")
    print(f"  {'종목':8s}  {'키움':>12}  {'KIS':>12}  {'괴리율':>8}")
    print(f"  {'─'*8}  {'─'*12}  {'─'*12}  {'─'*8}")

    all_ok = True
    for sym in TEST_SYMBOLS:
        kp = kiwoom.get(sym)
        kp_kis = kis.get(sym)
        if kp and kp_kis and kp_kis > 0:
            div = abs(kp / kp_kis - 1.0) * 100
            flag = "⚠️" if div > 1.0 else "  "
            print(f"  {flag}{sym}  {_fmt_price(kp):>12}  {_fmt_price(kp_kis):>12}  {div:>7.2f}%")
            if div > 1.0:
                all_ok = False
        else:
            print(f"    {sym}  {_fmt_price(kp):>12}  {_fmt_price(kp_kis):>12}  {'n/a':>8}")

    if all_ok:
        print("\n  ✅ 모든 가격 괴리율 1% 이내")
    else:
        print("\n  ⚠️  일부 종목 가격 괴리 1% 초과 — 실전 연결 전 원인 확인 필요")


# ── Step 5: Minute bars speed test ───────────────────────────────────────────

def test_minute_bars(auth) -> None:
    _bar("5. 분봉 조회 속도 테스트")
    from brokers.kiwoom.data_provider import KiwoomDataProvider
    provider = KiwoomDataProvider(auth)

    test_sym = "005930"
    t0 = time.perf_counter()
    bars = provider.get_minute_bars(test_sym, interval=1, lookback=10)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if bars:
        print(f"  ✅ {test_sym} 분봉 {len(bars)}개 수신 ({elapsed_ms:.0f}ms)")
        print(f"     최신봉: {bars[0]}")
    else:
        print(f"  ❌ {test_sym} 분봉 조회 실패 ({elapsed_ms:.0f}ms)")

    # Speed test: 10 symbols
    _bar("6. 10종목 현재가 속도 테스트")
    speed_symbols = TEST_SYMBOLS + ["028260", "207940", "006400", "068270", "003550"]
    result = provider.speed_test(speed_symbols)
    print(f"  성공: {len(result['successes'])}종목  실패: {len(result['failures'])}종목")
    print(f"  평균: {_fmt_ms(result['avg_ms'])}  최대: {_fmt_ms(result['max_ms'])}")
    if result["failures"]:
        print(f"  실패 종목: {result['failures']}")


# ── Step 6: KIS minute bar speed ─────────────────────────────────────────────

def test_kis_minute_bars() -> None:
    _bar("7. KIS 분봉 조회 속도 비교 (삼성전자)")
    test_sym = "005930"
    try:
        from brokers.kis.api_client import KISClient
        client = KISClient()
        t0 = time.perf_counter()
        result = client.get_intraday_ohlcv(test_sym, interval="1m", lookback=10)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if result is not None and len(result) > 0:
            print(f"  ✅ KIS 분봉 {len(result)}개 ({elapsed_ms:.0f}ms)")
        else:
            print(f"  ❌ KIS 분봉 없음 ({elapsed_ms:.0f}ms)")
    except Exception as exc:
        print(f"  ⚠️  KIS 분봉 실패: {exc}")


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(kiwoom_prices: dict, kis_prices: dict) -> None:
    _bar("요약 및 실전봇 연결 전 리스크")
    kiwoom_ok = sum(1 for v in kiwoom_prices.values() if v)
    kis_ok    = sum(1 for v in kis_prices.values() if v)
    total     = len(TEST_SYMBOLS)

    print(f"  키움 현재가: {kiwoom_ok}/{total} 성공")
    print(f"  KIS  현재가: {kis_ok}/{total} 성공")
    print()

    risks = []
    if kiwoom_ok < total:
        risks.append("❌ 키움 API 엔드포인트 미확인 — 응답 키 확인 후 kiwoom_data_provider.py 수정 필요")
    if kiwoom_ok == total and kis_ok == total:
        print("  ✅ 현재가 조회 정상 — 분봉 응답 확인 후 실전봇 연결 검토 가능")
    for r in risks:
        print(f"  {r}")
    print()
    print("  [남은 단계]")
    print("  1. 분봉 응답 구조 확인 후 kiwoom_data_provider.py 필드명 보정")
    print("  2. KIS 분봉과 속도 비교 수치 확인")
    print("  3. 실전봇 연결 여부는 사용자가 결정")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "═" * 60)
    print("  Kiwoom REST API 데이터 공급자 테스트")
    print("═" * 60)

    auth = test_token()
    if auth is None:
        print("\n토큰 발급 실패 — 중단합니다.")
        sys.exit(1)

    kiwoom_prices = test_kiwoom_prices(auth)
    kis_prices    = test_kis_prices()
    compare_prices(kiwoom_prices, kis_prices)
    test_minute_bars(auth)
    test_kis_minute_bars()
    print_summary(kiwoom_prices, kis_prices)
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
