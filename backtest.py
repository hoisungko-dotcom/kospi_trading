import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from rich.console import Console
from rich.table import Table
from rich import print as rprint
import FinanceDataReader as fdr

# 코어 전략 모듈 임포트
from core.api_client import KISClient
from core.market_data_kospi import MarketDataKOSPI
from core.signal_analyzer_kospi import SignalAnalyzerKospi
from core.risk_management import RiskManagement
from core.investor_flow import InvestorFlow

from core.dynamic_exit_analyzer_kospi import DynamicExitAnalyzerKospi

console = Console()

class KOSPIBacktester:
    """코스피 전략 백테스팅 엔진"""

    def __init__(self, initial_balance=10000000):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.holdings = {} # {symbol: {'qty': 0, 'entry_price': 0}}
        self.history = []
        self._kospi_index: pd.DataFrame | None = None   # 변동성 필터용 지수 데이터

        # 엔진 초기화
        self.client = KISClient()
        self.client.authenticate()
        self.market_data = MarketDataKOSPI(self.client)
        self.signal_engine = SignalAnalyzerKospi()
        self.risk_manager = RiskManagement()
        self.exit_analyzer = DynamicExitAnalyzerKospi()

    def _load_kospi_index(self, days: int):
        """변동성 필터용 KOSPI 지수 데이터 로드 (SMA20 + ATR14 포함)"""
        try:
            end   = datetime.now()
            start = end - timedelta(days=days + 80)
            df = fdr.DataReader('KS11',
                                start.strftime('%Y-%m-%d'),
                                end.strftime('%Y-%m-%d'))
            close = df['Close'].astype(float)
            high  = df['High'].astype(float)
            low   = df['Low'].astype(float)
            df['sma20']   = close.rolling(20).mean()
            tr            = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low  - close.shift()).abs(),
            ], axis=1).max(axis=1)
            df['atr14']   = tr.rolling(14).mean()
            df['vol_pct'] = df['atr14'] / close * 100
            self._kospi_index = df
        except Exception as e:
            rprint(f"[yellow]⚠️ KOSPI 지수 로드 실패 — 변동성 필터 비활성화: {e}[/yellow]")
            self._kospi_index = None

    def _market_ok(self, date_str: str) -> bool:
        """해당 날짜의 시장 조건(추세+변동성) 충족 여부"""
        if self._kospi_index is None:
            return True
        try:
            dt  = pd.Timestamp(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}")
            idx = self._kospi_index.index[self._kospi_index.index <= dt]
            if len(idx) == 0:
                return True
            row       = self._kospi_index.loc[idx[-1]]
            threshold = float(os.getenv('MARKET_VOL_THRESHOLD', '2.0'))
            trend_ok  = float(row['Close']) >= float(row['sma20'])
            vol_ok    = float(row['vol_pct']) < threshold
            return trend_ok and vol_ok
        except Exception:
            return True

    def run_backtest(self, symbol, days=60):
        rprint(f"\n[bold cyan]📈 {symbol} 백테스트 시작 (최근 {days}일 데이터)[/bold cyan]")

        # KOSPI 지수 데이터 로드 (최초 1회 또는 미로드 시)
        if self._kospi_index is None:
            self._load_kospi_index(days)

        # 1. 과거 데이터 수집 (충분한 분석을 위해 days + 100일)
        df = self.market_data.get_kospi_ohlcv(symbol, interval='1d', lookback=days + 100)
        if df is None or len(df) < 100:
            rprint("[red]데이터 부족으로 백테스트를 취소합니다.[/red]")
            return
        
        # 지표 미리 계산 (ATR 등)
        df = self.risk_manager.calculate_atr(df)
        close = df['close'].astype(float)
        volume = df['volume'].astype(float)
        df['sma_5'] = close.rolling(5).mean()
        df['sma_20'] = close.rolling(20).mean()
        df['sma_60'] = close.rolling(60).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['bb_middle'] = close.rolling(20).mean()
        df['bb_upper'] = df['bb_middle'] + 2 * close.rolling(20).std()
        df['avg_volume_20'] = volume.rolling(20).mean()
        df['close_5d_ago'] = close.shift(5)
        
        # 백테스트 구간 설정 (마지막 days일)
        test_df = df.iloc[-(days):].copy()
        
        trades = []
        current_equity = self.initial_balance
        
        for i in range(len(test_df)):
            # 현재 시점의 '과거 데이터' 슬라이싱 (당일 데이터 포함)
            current_idx = test_df.index[i]
            history_upto_now = df.loc[:current_idx]

            current_price = test_df['close'].iloc[i]
            date_str = test_df['date'].iloc[i]
            atr = test_df['ATR'].iloc[i]

            # 연속 매집 일수 (InvestorFlow 수급 프록시)
            ph = history_upto_now[['open', 'high', 'low', 'close', 'volume']].tail(30).to_dict('records')
            consecutive_buy_days = InvestorFlow.buy_pressure_days(ph)

            price_data = {
                'close': current_price,
                'open': test_df['open'].iloc[i],
                'high': test_df['high'].iloc[i],
                'low': test_df['low'].iloc[i],
                'volume': test_df['volume'].iloc[i],
                'atr': atr,
                'sma_5': test_df['sma_5'].iloc[i],
                'sma_20': test_df['sma_20'].iloc[i],
                'sma_60': test_df['sma_60'].iloc[i],
                'rsi': test_df['rsi'].iloc[i] if pd.notna(test_df['rsi'].iloc[i]) else 50,
                'macd': test_df['macd'].iloc[i],
                'macd_signal': test_df['macd_signal'].iloc[i],
                'bb_middle': test_df['bb_middle'].iloc[i],
                'bb_upper': test_df['bb_upper'].iloc[i],
                'avg_volume_20': test_df['avg_volume_20'].iloc[i],
                'close_5d_ago':        test_df['close_5d_ago'].iloc[i],
                'consecutive_buy_days': consecutive_buy_days,
            }

            final_score = self.signal_engine.calculate_score(symbol, price_data)
            signal = self.signal_engine.detect_signal(symbol, price_data)
            
            # 3. 매매 로직
            # 매수: 신호 발생 + 미보유 + 시장 조건 충족
            if signal == 'BUY' and symbol not in self.holdings and self._market_ok(date_str):
                stop_loss = current_price - (atr * 2.0)
                stop_loss = max(stop_loss, current_price * 0.97)
                
                qty = self.risk_manager.calculate_position_size(current_equity, 0.02, current_price, stop_loss)
                
                if qty > 0 and self.balance >= qty * current_price:
                    self.holdings[symbol] = {
                        'qty': qty,
                        'entry_price': current_price,
                        'entry_date': date_str,
                        'stop_loss': stop_loss,
                        'highest_price': current_price
                    }
                    self.balance -= qty * current_price
                    rprint(f"  [green]BUY[/green]  | {date_str} | {current_price:,.0f}원 | {qty}주 매수 (Score: {final_score:.1f}점)")
            
            # 매도: 보유 중 + 익절/손절 판단
            elif symbol in self.holdings:
                holding = self.holdings[symbol]
                holding['highest_price'] = max(holding['highest_price'], current_price)
                profit_pct = (current_price - holding['entry_price']) / holding['entry_price']
                
                # Trailing Stop
                ts_price = self.risk_manager.trailing_stop(current_price, holding['highest_price'], atr, multiplier=2.5)
                
                # Dynamic Exit (main.py 로직)
                trend_score = self.exit_analyzer.assess_trend_strength(symbol, price_data)
                should_tp, tp_reason, _ = self.exit_analyzer.calculate_dynamic_take_profit(
                    symbol, holding['entry_price'], current_price, trend_score, atr=atr
                )
                
                is_sell = False
                reason = ""
                
                if current_price <= holding['stop_loss']:
                    is_sell = True; reason = "STOP_LOSS"
                elif current_price < ts_price and profit_pct > 0.02:
                    is_sell = True; reason = "TRAILING_STOP"
                elif should_tp:
                    is_sell = True; reason = f"PROFIT:{tp_reason}"
                
                if is_sell:
                    profit_val = (current_price - holding['entry_price']) * holding['qty']
                    self.balance += current_price * holding['qty']
                    trades.append({
                        'entry_date': holding['entry_date'],
                        'exit_date': date_str,
                        'profit_pct': profit_pct,
                        'profit_val': profit_val,
                        'reason': reason
                    })
                    color = "blue" if profit_val > 0 else "red"
                    rprint(f"  [bold {color}]SELL[/bold {color}] | {date_str} | {current_price:,.0f}원 | {reason} ({profit_pct:.2%})")
                    del self.holdings[symbol]
            
            current_equity = self.balance + (self.holdings[symbol]['qty'] * current_price if symbol in self.holdings else 0)


        self.display_summary(symbol, trades, current_equity)

    def display_summary(self, symbol, trades, final_equity):
        total_trades = len(trades)
        if total_trades == 0:
            rprint(f"\n[yellow]⚠️ {symbol}: 백테스트 기간 중 발생한 거래가 없습니다.[/yellow]")
            return

        wins = [t for t in trades if t['profit_val'] > 0]
        win_rate = len(wins) / total_trades
        total_profit = final_equity - self.initial_balance
        return_pct = (total_profit / self.initial_balance) * 100

        table = Table(title=f"📊 {symbol} 백테스트 결과 요약", show_header=True, header_style="bold magenta")
        table.add_column("항목", justify="left")
        table.add_column("값", justify="right")
        
        table.add_row("총 거래 횟수", f"{total_trades}회")
        table.add_row("승률", f"{win_rate:.2%}")
        table.add_row("최종 자산", f"{final_equity:,.0f}원")
        table.add_row("누적 수익금", f"{total_profit:,.0f}원")
        table.add_row("누적 수익률", f"[bold {'green' if return_pct > 0 else 'red'}]{return_pct:.2f}%[/bold {'green' if return_pct > 0 else 'red'}]")
        
        console.print(table)

if __name__ == "__main__":
    import time as _time
    from dotenv import load_dotenv
    load_dotenv()

    # KOSPI 상위 100 + KOSDAQ 상위 50 (거래대금 50억 이상)
    try:
        import FinanceDataReader as fdr
        import pandas as _pd
        _kospi  = fdr.StockListing('KOSPI').sort_values('Marcap', ascending=False).head(100)
        _kosdaq = fdr.StockListing('KOSDAQ').sort_values('Marcap', ascending=False).head(50)
        _combined = _pd.concat([_kospi, _kosdaq]).query("Amount >= 5_000_000_000")
        symbols = _combined['Code'].tolist()
        rprint(f"[cyan]📋 대상 종목: KOSPI 상위 100 + KOSDAQ 상위 50 → 거래대금 필터 후 {len(symbols)}개[/cyan]")
    except Exception as _e:
        rprint(f"[yellow]⚠️ 종목 목록 자동 수집 실패 ({_e}), 기본 20개로 실행[/yellow]")
        symbols = [
            '005930', '000660', '005380', '000270', '068270',
            '005490', '035420', '035720', '006400', '051910',
            '000100', '034220', '010130', '009540', '051900',
            '011070', '047050', '004020', '024110', '071050',
        ]

    tester = KOSPIBacktester()
    all_trades = []
    t0 = _time.time()

    for i, sym in enumerate(symbols, 1):
        tester.run_backtest(sym, days=500)
        tester.balance  = tester.initial_balance
        tester.holdings = {}
        if i % 30 == 0:
            elapsed = _time.time() - t0
            rprint(f"[dim]  {i}/{len(symbols)} 완료 ({elapsed:.0f}초)[/dim]")

    rprint(f"\n[bold green]✅ 전체 백테스트 완료 ({_time.time()-t0:.0f}초)[/bold green]")
