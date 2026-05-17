import pandas as pd
import numpy as np

class VolatilityAnalysis:
    """변동성 압축 (Squeeze) 탐지"""
    
    @staticmethod
    def calculate_bollinger_bands(df, period=20, std_dev=2):
        df['BB_Middle'] = df['close'].rolling(period).mean()
        df['BB_Std'] = df['close'].rolling(period).std()
        df['BB_Upper'] = df['BB_Middle'] + (df['BB_Std'] * std_dev)
        df['BB_Lower'] = df['BB_Middle'] - (df['BB_Std'] * std_dev)
        df['BB_Width'] = df['BB_Upper'] - df['BB_Lower']
        return df

    @staticmethod
    def detect_squeeze(df):
        """변동성 압축 감지: 밴드가 이전 평균 대비 50% 이하로 수축"""
        df = VolatilityAnalysis.calculate_bollinger_bands(df)
        df['BB_Width_MA'] = df['BB_Width'].rolling(20).mean()
        df['Squeeze_Ratio'] = df['BB_Width'] / df['BB_Width_MA']
        df['is_squeeze'] = df['Squeeze_Ratio'] < 0.5
        return df

    @staticmethod
    def get_vol_score(df):
        if 'is_squeeze' not in df.columns: return 0.5
        return 0.8 if df['is_squeeze'].iloc[-1] else 0.5
