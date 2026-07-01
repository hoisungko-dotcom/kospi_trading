"""
눌림목진입 A/B 백테스트 (FinanceDataReader 버전)
  A: 기존 전략  — PULLBACK_ENTRY_MIN_PCT=999  (눌림목 완화 비활성화)
  B: 신규 전략  — PULLBACK_ENTRY_MIN_PCT=3.0  (눌림목 완화 활성화)
기간: 최근 90일
대상: 로그 빈출 종목 + KOSPI/KOSDAQ 상위
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, '/home/ubuntu/kospi_trading_system')

import pandas as pd
import numpy as np
import FinanceDataReader as fdr
from datetime import datetime, timedelta

from strategy.signal_analyzer import SignalAnalyzerKospi
from core.investor_flow import InvestorFlow
from strategy.risk import RiskManagement

INITIAL = 10_000_000
DAYS    = 90

SYMBOLS = [
    '036570','034220','009150','011070','181710',
    '420770','007390','001740','018260','001820',
    '005930','000660','005380','000270','068270',
    '005490','035420','035720','006400','051910',
    '000100','010130','009540','051900','047050',
    '089970','036540','007810','003230','028260',
]

# ── KOSPI 지수 (시장국면 판단) ────────────────────────────────────────────
def load_kospi_index():
    end   = datetime.now()
    start = end - timedelta(days=DAYS + 100)
    df = fdr.DataReader('KS11', start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
    c, h, l = df['Close'].astype(float), df['High'].astype(float), df['Low'].astype(float)
    df['sma20']   = c.rolling(20).mean()
    tr            = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    df['atr14']   = tr.rolling(14).mean()
    df['vol_pct'] = df['atr14'] / c * 100
    return df

KOSPI_DF = load_kospi_index()

def get_market_mode(date):
    try:
        idx = KOSPI_DF.index[KOSPI_DF.index <= pd.Timestamp(date)]
        if len(idx) == 0: return False, False
        row = KOSPI_DF.loc[idx[-1]]
        c, s20, vp = float(row['Close']), float(row['sma20']), float(row['vol_pct'])
        gap = (c - s20) / s20 * 100 if s20 else 0
        strong = c >= s20 and gap >= 5.0
        selective = strong and vp > 2.5
        return strong, selective
    except Exception:
        return False, False

# ── 데이터 로드 (FDR) ─────────────────────────────────────────────────────
def load_stock(symbol):
    end   = datetime.now()
    start = end - timedelta(days=DAYS + 120)
    df = fdr.DataReader(symbol, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
    if df is None or len(df) < DAYS + 40:
        return None
    df = df.rename(columns={'Open':'open','High':'high','Low':'low',
                             'Close':'close','Volume':'volume'})
    df.index = pd.to_datetime(df.index)
    return df

# ── 지표 계산 ─────────────────────────────────────────────────────────────
def compute_indicators(df):
    df = df.copy()
    c, h, v = df['close'].astype(float), df['high'].astype(float), df['volume'].astype(float)
    df['sma_5']    = c.rolling(5).mean()
    df['sma_20']   = c.rolling(20).mean()
    df['sma_60']   = c.rolling(60).mean()
    df['high_20d'] = h.rolling(20).max()
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df['rsi']      = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    df['rsi_prev'] = df['rsi'].shift(1)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df['macd']             = ema12 - ema26
    df['macd_signal']      = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_prev']        = df['macd'].shift(1)
    df['macd_signal_prev'] = df['macd_signal'].shift(1)
    df['bb_middle']     = c.rolling(20).mean()
    df['bb_upper']      = df['bb_middle'] + 2 * c.rolling(20).std()
    df['avg_volume_20'] = v.rolling(20).mean()
    df['close_5d_ago']  = c.shift(5)
    tr = pd.concat([h-df['low'].astype(float),
                    (h-c.shift()).abs(),
                    (df['low'].astype(float)-c.shift()).abs()],axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    return df

# ── 단일 종목 백테스트 ────────────────────────────────────────────────────
def run_one(symbol, df_full, analyzer, pullback_on: bool):
    df   = compute_indicators(df_full)
    test = df.iloc[-DAYS:].copy()
    rm   = RiskManagement()

    balance  = INITIAL
    holdings = {}
    trades   = []

    for i in range(len(test)):
        hist  = df.loc[:test.index[i]]
        cur   = test.iloc[i]
        price = float(cur['close'])
        date  = test.index[i]
        atr   = float(cur['atr']) if pd.notna(cur['atr']) else price * 0.02

        ph  = hist[['open','high','low','close','volume']].tail(30)
        ph  = ph.rename(columns=str.lower).to_dict('records')
        cbd = InvestorFlow.buy_pressure_days(ph)

        pd_ = {
            'close': price, 'open': float(cur['open']),
            'high': float(cur['high']), 'low': float(cur['low']),
            'volume': float(cur['volume']), 'atr': atr,
            'sma_5':   float(cur['sma_5'])   if pd.notna(cur['sma_5'])   else price,
            'sma_20':  float(cur['sma_20'])  if pd.notna(cur['sma_20'])  else price,
            'sma_60':  float(cur['sma_60'])  if pd.notna(cur['sma_60'])  else price,
            'high_20d':float(cur['high_20d'])if pd.notna(cur['high_20d'])else price,
            'rsi':     float(cur['rsi'])     if pd.notna(cur['rsi'])     else 50,
            'rsi_prev':float(cur['rsi_prev'])if pd.notna(cur['rsi_prev'])else 50,
            'macd':    float(cur['macd'])    if pd.notna(cur['macd'])    else 0,
            'macd_signal': float(cur['macd_signal']) if pd.notna(cur['macd_signal']) else 0,
            'macd_prev':   float(cur['macd_prev'])   if pd.notna(cur['macd_prev'])   else 0,
            'macd_signal_prev': float(cur['macd_signal_prev']) if pd.notna(cur['macd_signal_prev']) else 0,
            'bb_middle':  float(cur['bb_middle'])  if pd.notna(cur['bb_middle'])  else price,
            'bb_upper':   float(cur['bb_upper'])   if pd.notna(cur['bb_upper'])   else price*1.1,
            'avg_volume_20': float(cur['avg_volume_20']) if pd.notna(cur['avg_volume_20']) else float(cur['volume']),
            'close_5d_ago':  float(cur['close_5d_ago'])  if pd.notna(cur['close_5d_ago'])  else price,
            'consecutive_buy_days': cbd,
        }

        os.environ['PULLBACK_ENTRY_MIN_PCT'] = '3.0' if pullback_on else '999'
        sm, sel = get_market_mode(date)
        signal, _ = analyzer.detect_signal(symbol, pd_, strong_market=sm, selective=sel)

        if signal == 'BUY' and symbol not in holdings:
            sl  = max(price - atr * 2.0, price * 0.97)
            qty = rm.calculate_position_size(
                balance + sum(h['qty']*price for h in holdings.values()),
                0.02, price, sl
            )
            if qty > 0 and balance >= qty * price:
                holdings[symbol] = {'qty': qty, 'entry': price, 'date': str(date.date()),
                                     'sl': sl, 'peak': price,
                                     'pullback_entry': pullback_on}
                balance -= qty * price

        elif symbol in holdings:
            h = holdings[symbol]
            h['peak'] = max(h['peak'], price)
            pct = (price - h['entry']) / h['entry']
            ts  = h['peak'] * 0.93
            sell = False
            if price <= h['sl']:              sell = True
            elif price < ts and pct > 0.02:   sell = True
            elif pct >= 0.15:                 sell = True
            if sell:
                val = (price - h['entry']) * h['qty']
                balance += price * h['qty']
                trades.append({'pct': pct, 'val': val, 'sym': symbol, 'date': str(date.date())})
                del holdings[symbol]

    # 미청산
    if holdings.get(symbol):
        h = holdings[symbol]
        fp = float(test['close'].iloc[-1])
        val = (fp - h['entry']) * h['qty']
        balance += fp * h['qty']
        trades.append({'pct': (fp-h['entry'])/h['entry'], 'val': val,
                       'sym': symbol, 'date': 'OPEN'})

    return trades

# ── 결과 출력 ─────────────────────────────────────────────────────────────
def print_result(label, tag, trades):
    if not trades:
        print(f"  [{label}] {tag}: 거래 없음\n"); return
    n    = len(trades)
    wins = [t for t in trades if t['val'] > 0]
    wr   = len(wins)/n
    avg  = sum(t['pct'] for t in trades)/n*100
    pf_w = sum(t['val'] for t in wins) or 0
    pf_l = abs(sum(t['val'] for t in trades if t['val'] <= 0)) or 1
    pf   = pf_w/pf_l
    tot  = sum(t['val'] for t in trades)
    print(f"  [{label}] {tag}")
    print(f"       총 거래: {n}회  |  승률: {wr:.1%}  |  평균수익: {avg:+.2f}%")
    print(f"       수익계수(PF): {pf:.2f}  |  총 수익금: ₩{tot:,.0f}")
    print()

# ── main ─────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"  눌림목진입 A/B 백테스트  |  최근 {DAYS}일  |  {len(SYMBOLS)}개 종목")
    print(f"  기준일: {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*60}\n")

    analyzer = SignalAnalyzerKospi()
    all_a, all_b = [], []
    skipped = []

    for i, sym in enumerate(SYMBOLS, 1):
        print(f"  [{i:2d}/{len(SYMBOLS)}] {sym} ...", end='\r')
        try:
            df = load_stock(sym)
            if df is None:
                skipped.append(sym); continue
            all_a.extend(run_one(sym, df, analyzer, pullback_on=False))
            all_b.extend(run_one(sym, df, analyzer, pullback_on=True))
        except Exception as e:
            skipped.append(f"{sym}({type(e).__name__}:{e})")

    print(f"\n  완료. 스킵: {len(skipped)}개  {skipped[:3] if skipped else ''}\n")
    print_result('A', '기존 (눌림목OFF)', all_a)
    print_result('B', '신규 (눌림목ON) ', all_b)

    # 상세 비교
    diff_trades = len(all_b) - len(all_a)
    diff_pf_a = (sum(t['val'] for t in all_a if t['val']>0)/
                 max(abs(sum(t['val'] for t in all_a if t['val']<=0)),1)) if all_a else 0
    diff_pf_b = (sum(t['val'] for t in all_b if t['val']>0)/
                 max(abs(sum(t['val'] for t in all_b if t['val']<=0)),1)) if all_b else 0

    print(f"  ── 비교 요약 ──────────────────────────────────")
    print(f"  거래수 변화: {diff_trades:+d}회")
    print(f"  수익계수:    A={diff_pf_a:.2f}  →  B={diff_pf_b:.2f}  ({diff_pf_b-diff_pf_a:+.2f})")
    if all_a and all_b:
        wr_a = len([t for t in all_a if t['val']>0])/len(all_a)
        wr_b = len([t for t in all_b if t['val']>0])/len(all_b)
        print(f"  승  률:      A={wr_a:.1%}  →  B={wr_b:.1%}  ({wr_b-wr_a:+.1%})")
        avg_a = sum(t['pct'] for t in all_a)/len(all_a)*100
        avg_b = sum(t['pct'] for t in all_b)/len(all_b)*100
        print(f"  평균수익:    A={avg_a:+.2f}%  →  B={avg_b:+.2f}%  ({avg_b-avg_a:+.2f}%p)")
    print(f"\n{'='*60}\n")

if __name__ == '__main__':
    main()
