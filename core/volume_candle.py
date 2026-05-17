import pandas as pd
import numpy as np

class VolumeCandleStrategy:
    """거래량 스파이크 및 캔들 패턴 인식"""
    
    @staticmethod
    def detect_volume_spike(df):
        """20일 평균 대비 150% 이상의 거래량 + 양봉"""
        df['Volume_MA20'] = df['volume'].rolling(20).mean()
        df['Volume_Spike'] = (
            (df['volume'] > df['Volume_MA20'] * 1.5) & 
            (df['close'] > df['open'])
        )
        return df

    @staticmethod
    def detect_hammer(df):
        """망치형 (Hammer): 저가매수 신호"""
        df['body'] = abs(df['close'] - df['open'])
        df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']
        df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
        
        df['is_hammer'] = (
            (df['lower_wick'] > df['body'] * 2) &
            (df['upper_wick'] < df['body'] * 0.5) &
            (df['body'] < df['lower_wick'] * 0.3)
        )
        return df

    @staticmethod
    def detect_engulfing(df):
        """포장형 (Engulfing): 음봉 다음에 고가/저가를 모두 잡아먹는 양봉"""
        df['is_bullish_engulfing'] = False
        for i in range(1, len(df)):
            if (df['close'].iloc[i-1] < df['open'].iloc[i-1] and # 음봉
                df['close'].iloc[i] > df['open'].iloc[i] and     # 양봉
                df['high'].iloc[i] > df['high'].iloc[i-1] and
                df['low'].iloc[i] < df['low'].iloc[i-1]):
                df.at[df.index[i], 'is_bullish_engulfing'] = True
        return df

    @staticmethod
    def get_combined_score(df):
        if df.empty: return 0.5
        latest = df.iloc[-1]
        score = 0.5
        if latest.get('Volume_Spike'): score += 0.2
        if latest.get('is_hammer'): score += 0.15
        if latest.get('is_bullish_engulfing'): score += 0.15
        return min(1.0, score)
