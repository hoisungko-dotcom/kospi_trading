import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

# .env 로드 (단독 실행 시 환경변수 주입)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass


def format_currency(value):
    return f"₩{value:,.0f}"


def perform_sync():
    """KIS API와 동기화 수행"""
    try:
        from core.kis_client_kospi import KISClientKospi
        from core.position_manager import PositionManager

        print("🔄 KIS API와 계좌 정보를 동기화 중...")
        kis     = KISClientKospi()
        balance = kis.get_balance()

        if balance:
            pm = PositionManager()
            pm.sync_from_api(
                holdings=balance.get('holdings', {}),
                cash=balance.get('cash', 0)
            )
            print("✅ 동기화 완료!")
            return True
        else:
            print("⚠️ 동기화 실패 (API 응답 없음)")
            return False
    except Exception as e:
        print(f"❌ 동기화 중 오류 발생: {e}")
        return False


def _get_live_prices(holdings: dict) -> dict:
    """KIS API로 보유 종목 현재가 조회. 실패 시 빈 dict."""
    if not holdings:
        return {}
    try:
        from core.api_client import KISClient
        client = KISClient()
        client.authenticate()
        prices = {}
        for sym in holdings:
            p = client.get_kr_current_price(sym)
            if p > 0:
                prices[sym] = p
            time.sleep(0.05)
        return prices
    except Exception as e:
        print(f"  (현재가 조회 실패: {e})")
        return {}


def show_status(live: bool = False):
    project_dir    = Path(__file__).parent
    log_dir        = project_dir / "paper_trading_logs"
    today          = datetime.now().strftime('%Y%m%d')
    portfolio_path = log_dir / f"portfolio_{today}.json"

    if not portfolio_path.exists():
        yesterday      = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
        portfolio_path = log_dir / f"portfolio_{yesterday}.json"

    if not portfolio_path.exists():
        print("❌ 포트폴리오 데이터를 찾을 수 없습니다. 동기화를 먼저 시도해주세요.")
        return

    with open(portfolio_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    cash     = data.get('cash', 0)
    holdings = data.get('holdings', {})
    initial  = data.get('initial_capital', 10_000_000)

    # 현재가 조회 (--live 또는 live=True 시)
    live_prices: dict = {}
    if live:
        print("📡 KIS API에서 현재가 조회 중...")
        live_prices = _get_live_prices(holdings)

    # 평가금액 계산
    eval_total = 0.0
    for sym, h in holdings.items():
        cur = live_prices.get(sym, h.get('last_price', h.get('price', 0)))
        eval_total += cur * h.get('quantity', 0)

    total_value = cash + eval_total
    profit      = total_value - initial
    profit_pct  = (profit / initial) * 100 if initial else 0

    print()
    print("=" * 70)
    print(f"📊 KOSPI/KOSDAQ 계좌 현황  (기준: {data.get('timestamp', '?')[:19]})")
    print("=" * 70)
    print(f"  {'예수금':<20} {format_currency(cash):>18}")
    print(f"  {'주식 평가금액':<20} {format_currency(eval_total):>18}")
    print(f"  {'총 자산':<20} {format_currency(total_value):>18}")
    print(f"  {'누적 수익':<20} {format_currency(profit):>18}  ({profit_pct:+.2f}%)")
    print("-" * 70)

    if not holdings:
        print("  보유 종목 없음")
    else:
        hdr = f"  {'종목코드':<8} | {'수량':>5} | {'평균단가':>11} | {'매수금액':>13}"
        if live_prices:
            hdr += f" | {'현재가':>11} | {'평가손익':>12} | {'수익률':>7}"
        print(hdr)
        print("  " + "-" * (66 if not live_prices else 116))

        for sym, h in holdings.items():
            qty     = h.get('quantity', 0)
            avg_p   = h.get('price', 0)
            amt     = h.get('amount', avg_p * qty)
            row     = f"  {sym:<8} | {qty:>5,} | {format_currency(avg_p):>13} | {format_currency(amt):>15}"

            if live_prices:
                cur_p  = live_prices.get(sym, 0)
                if cur_p > 0:
                    pfls   = (cur_p - avg_p) * qty
                    pfls_r = (cur_p - avg_p) / avg_p * 100 if avg_p else 0
                    row   += f" | {format_currency(cur_p):>13} | {format_currency(pfls):>14} | {pfls_r:>+6.2f}%"
                else:
                    row += f" | {'조회실패':>13} | {'-':>14} | {'-':>7}"
            print(row)

    print("=" * 70)
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    do_sync = 'sync' in args
    do_live = 'live' in args

    if do_sync:
        perform_sync()

    show_status(live=do_live)
