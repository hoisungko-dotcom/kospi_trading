import pandas as pd
import numpy as np

class IchimokuStrategy:
    """일목균형표 전략 - 종합 추세 분석"""
    
    @staticmethod
    def calculate_ichimoku(df, tenkan=9, kijun=26, senkou=52):
        # 1. 전환선 (Tenkan)
        tenkan_high = df['high'].rolling(window=tenkan).max()
        tenkan_low = df['low'].rolling(window=tenkan).min()
        df['Tenkan'] = (tenkan_high + tenkan_low) / 2
        
        # 2. 기준선 (Kijun)
        kijun_high = df['high'].rolling(window=kijun).max()
        kijun_low = df['low'].rolling(window=kijun).min()
        df['Kijun'] = (kijun_high + kijun_low) / 2
        
        # 3. 선행스팬1 (Senkou Span 1)
        df['Senkou_1'] = ((df['Tenkan'] + df['Kijun']) / 2).shift(26)
        
        # 4. 선행스팬2 (Senkou Span 2)
        senkou2_high = df['high'].rolling(window=senkou).max()
        senkou2_low = df['low'].rolling(window=senkou).min()
        df['Senkou_2'] = ((senkou2_high + senkou2_low) / 2).shift(26)
        
        # 5. 후행선 (Chikou Span)
        df['Chikou'] = df['close'].shift(-26)
        
        return df
    
    @staticmethod
    def generate_ichimoku_signal(df):
        """매수 신호: 종가 > 구름 & 전환선 > 기준선 & 후행선 확인"""
        df['Ichimoku_Signal'] = 'NONE'
        if len(df) < 100: return df
        
        for i in range(100, len(df)):
            cloud_top = max(df['Senkou_1'].iloc[i], df['Senkou_2'].iloc[i])
            
            # 구름 위에 있고, 전환선 > 기준선
            if (df['close'].iloc[i] > cloud_top and 
                df['Tenkan'].iloc[i] > df['Kijun'].iloc[i]):
                
                # 후행선이 가격보다 위에 있는지 확인 (강력한 매수)
                if i+26 < len(df) and df['Chikou'].iloc[i] > df['close'].iloc[i]:
                    df.at[df.index[i], 'Ichimoku_Signal'] = 'STRONG_BUY'
                else:
                    df.at[df.index[i], 'Ichimoku_Signal'] = 'BUY'
        return df
