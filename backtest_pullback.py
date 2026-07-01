"""
눌림목진입 A/B 백테스트
  A: 기존 전략 (PULLBACK_ENTRY_MIN_PCT=999 — 눌림목 완화 비활성화)
  B: 신규 전략 (PULLBACK_ENTRY_MIN_PCT=3.0  — 눌림목 완화 활성화)
기간: 최근 90일 (KOSPI 강세장 구간 포함)
대상: 로그에 자주 등장한 종목 + KOSPI 상위 30
"""
import os, sys
os.environ.setdefault("KIS_URL", "https://openapi.koreainvestment.com:9443")

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import FinanceDataReader as fdr
sys.path.insert(0, '/home/ubuntu/kospi_trading_system')
from strategy.signal_analyzer import SignalAnalyzerKospi
from core.investor_flow import InvestorFlow
from strategy.risk import RiskManagement

INITIAL = 10_000_000
DAYS    = 90

# 최근 로그에서 자주 나온 종목 + KOSPI 상위
SYMBOLS = [
    # 로그 빈출
    '036570', '034220', '009150', '011070', '181710',
    '420770', '007390', '001740', '018260', '001820',
    # KOSPI 대형주
    '005930', '000660', '005380', '000270', '068270',
    '005490', '035420', '035720', '006400', '051910',
    '000100', '010130', '009540', '051900', '047050',
    '089970', '036540', '007810', '003230', '028260',
]

# ── KOSPI 지수 (시장국면 판단용) ──────────────────────────────────────────
def load_kospi_index(days):
    end   = datetime.now()
    start = end - timedelta(days=days + 80)
    df = fdr.DataReader('KS11', start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
    close = df['Close'].astype(float)
    high  = df['High'].astype(float)
    low   = df['Low'].astype(float)
    df['sma20']   = close.rolling(20).mean()
    tr = pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
    df['atr14']   = tr.rolling(14).mean()
    df['vol_pct'] = df['atr14'] / close * 100
    return df

def market_mode(kospi_df, date):
    try:
        dt  = pd.Timestamp(date[:4]+'-'+date[4:6]+'-'+date[6:8])
        idx = kospi_df.index[kospi_df.index <= dt]
        if len(idx) == 0: return False, False
        row = kospi_df.loc[idx[-1]]
        c, s20, vp = float(row['Close']), float(row['sma20']), float(row['vol_pct'])
        trend_ok = c >= s20
        gap_pct  = (c - s20) / s20 * 100 if s20 else 0
        strong_market = trend_ok and gap_pct >= 5.0
        # 변동성 2.5 이하면 non-selective, 초과면 selective
        selective = strong_market and vp > 2.5
        return strong_market, selective
    except Exception:
        return False, False

# ── 지표 계산 ─────────────────────────────────────────────────────────────
def compute_indicators(df):
    df = df.copy()
    close  = df['close'].astype(float)
    high   = df['high'].astype(float)
    volume = df['volume'].astype(float)
    df['sma_5']   = close.rolling(5).mean()
    df['sma_20']  = close.rolling(20).mean()
    df['sma_60']  = close.rolling(60).mean()
    df['high_20d']= high.rolling(20).max()
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = -delta.clip(upper=0).rolling(14).mean()
    df['rsi']     = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    df['rsi_prev']= df['rsi'].shift(1)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['macd']         = ema12 - ema26
    df['macd_signal']  = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_prev']    = df['macd'].shift(1)
    df['macd_signal_prev'] = df['macd_signal'].shift(1)
    df['bb_middle'] = close.rolling(20).mean()
    df['bb_upper']  = df['bb_middle'] + 2 * close.rolling(20).std()
    df['avg_volume_20'] = volume.rolling(20).mean()
    df['close_5d_ago']  = close.shift(5)
    # ATR
    tr = pd.concat([high-df['low'].astype(float),
                    (high-close.shift()).abs(),
                    (df['low'].astype(float)-close.shift()).abs()],axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    return df

# ── 단일 종목 백테스트 ────────────────────────────────────────────────────
def run_one(symbol, df_full, kospi_df, analyzer, pullback_on: bool):
    df = compute_indicators(df_full)
    test = df.iloc[-DAYS:].copy()
    rm   = RiskManagement()

    balance  = INITIAL
    holdings = {}
    trades   = []
    equity   = INITIAL

    for i in range(len(test)):
        hist     = df.loc[:test.index[i]]
        cur      = test.iloc[i]
        price    = float(cur['close'])
        date_str = str(cur['date']) if 'date' in cur else test.index[i].strftime('%Y%m%d')
        atr      = float(cur['atr']) if pd.notna(cur['atr']) else price * 0.02

        ph = hist[['open','high','low','close','volume']].tail(30).to_dict('records')
        cbd = InvestorFlow.buy_pressure_days(ph)

        pd_ = {
            'close': price, 'open': float(cur['open']),
            'high': float(cur['high']), 'low': float(cur['low']),
            'volume': float(cur['volume']), 'atr': atr,
            'sma_5': float(cur['sma_5'])   if pd.notna(cur['sma_5'])  else price,
            'sma_20': float(cur['sma_20']) if pd.notna(cur['sma_20']) else price,
            'sma_60': float(cur['sma_60']) if pd.notna(cur['sma_60']) else price,
            'high_20d': float(cur['high_20d']) if pd.notna(cur['high_20d']) else price,
            'rsi': float(cur['rsi']) if pd.notna(cur['rsi']) else 50,
            'rsi_prev': float(cur['rsi_prev']) if pd.notna(cur['rsi_prev']) else 50,
            'macd': float(cur['macd']) if pd.notna(cur['macd']) else 0,
            'macd_signal': float(cur['macd_signal']) if pd.notna(cur['macd_signal']) else 0,
            'macd_prev': float(cur['macd_prev']) if pd.notna(cur['macd_prev']) else 0,
            'macd_signal_prev': float(cur['macd_signal_prev']) if pd.notna(cur['macd_signal_prev']) else 0,
            'bb_middle': float(cur['bb_middle']) if pd.notna(cur['bb_middle']) else price,
            'bb_upper': float(cur['bb_upper'])  if pd.notna(cur['bb_upper'])  else price*1.1,
            'avg_volume_20': float(cur['avg_volume_20']) if pd.notna(cur['avg_volume_20']) else float(cur['volume']),
            'close_5d_ago': float(cur['close_5d_ago']) if pd.notna(cur['close_5d_ago']) else price,
            'consecutive_buy_days': cbd,
        }

        # 눌림목 완화 ON/OFF
        os.environ['PULLBACK_ENTRY_MIN_PCT'] = '3.0' if pullback_on else '999'

        sm, sel = market_mode(kospi_df, date_str)
        signal, _ = analyzer.detect_signal(symbol, pd_, strong_market=sm, selective=sel)

        # 매수
        if signal == 'BUY' and symbol not in holdings:
            sl  = max(price - atr * 2.0, price * 0.97)
            qty = rm.calculate_position_size(equity, 0.02, price, sl)
            if qty > 0 and balance >= qty * price:
                holdings[symbol] = {'qty': qty, 'entry': price, 'date': date_str,
                                     'sl': sl, 'peak': price}
                balance -= qty * price

        # 보유 중 익절/손절
        elif symbol in holdings:
            h = holdings[symbol]
            h['peak'] = max(h['peak'], price)
            pct = (price - h['entry']) / h['entry']
            ts  = h['peak'] * 0.93   # trailing 7%
            sell = False
            if price <= h['sl']:                    sell = True
            elif price < ts and pct > 0.02:         sell = True
            elif pct >= 0.15:                       sell = True
            if sell:
                val = (price - h['entry']) * h['qty']
                balance += price * h['qty']
                trades.append({'pct': pct, 'val': val, 'date': date_str})
                del holdings[symbol]

        equity = balance + (holdings[symbol]['qty'] * price if symbol in holdings else 0)

    # 미청산
    for sym, h in holdings.items():
        final_p = float(test['close'].iloc[-1])
        val = (final_p - h['entry']) * h['qty']
        balance += final_p * h['qty']
        trades.append({'pct': (final_p-h['entry'])/h['entry'], 'val': val, 'date': 'OPEN'})

    final_equity = balance
    return trades, final_equity

# ── 실행 ─────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"  눌림목진입 A/B 백테스트 | 최근 {DAYS}일 | {len(SYMBOLS)}개 종목")
    print(f"  기준일: {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*60}\n")

    from core.market_data_kospi import MarketDataKOSPI
    from brokers.kis.api_client import KISClient
    client = KISClient(); client.authenticate()
    md     = MarketDataKOSPI(client)
    analyzer = SignalAnalyzerKospi()
    kospi_df = load_kospi_index(DAYS)

    results = {'A': [], 'B': []}
    skipped = []

    for i, sym in enumerate(SYMBOLS, 1):
        print(f"  [{i:2d}/{len(SYMBOLS)}] {sym} 처리 중...", end='\r')
        try:
            df = md.get_kospi_ohlcv(sym, interval='1d', lookback=DAYS+120)
            if df is None or len(df) < DAYS + 60:
                skipped.append(sym); continue

            trades_a, eq_a = run_one(sym, df, kospi_df, analyzer, pullback_on=False)
            trades_b, eq_b = run_one(sym, df, kospi_df, analyzer, pullback_on=True)

            results['A'].extend(trades_a)
            results['B'].extend(trades_b)
        except Exception as e:
            skipped.append(f"{sym}({e})")

    print(f"\n  처리 완료. 스킵: {len(skipped)}개\n")
    if skipped: print(f"  스킵 목록: {skipped[:5]}...\n")

    for label, trades in results.items():
        tag = '기존 (눌림목OFF)' if label == 'A' else '신규 (눌림목ON)'
        if not trades:
            print(f"  [{label}] {tag}: 거래 없음\n"); continue

        n    = len(trades)
        wins = [t for t in trades if t['val'] > 0]
        wr   = len(wins)/n if n else 0
        avg  = sum(t['pct'] for t in trades)/n*100 if n else 0
        pf_w = sum(t['val'] for t in wins)
        pf_l = abs(sum(t['val'] for t in trades if t['val'] <= 0))
        pf   = pf_w/pf_l if pf_l else float('inf')
        print(f"  [{label}] {tag}")
        print(f"       총 거래: {n}회  |  승률: {wr:.1%}  |  평균수익: {avg:+.2f}%")
        print(f"       수익계수(PF): {pf:.2f}  |  총 수익금: ₩{sum(t['val'] for t in trades):,.0f}")
        # 눌림목 진입으로 새로 생긴 거래 식별 불가하므로 거래수 차이로 비교
        print()

    diff = len(results['B']) - len(results['A'])
    print(f"  거래수 차이 B-A: {diff:+d}회 (눌림목 완화로 추가된 거래)")
    print(f"\n{'='*60}\n")

if __name__ == '__main__':
    main()
