import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)

class SMCStrategy:
    """Smart Money Concept: 유동성 사냥 + 오더블록"""
    
    @staticmethod
    def detect_liquidity_sweep(df, lookback=20):
        """이전 저점을 뚫고 빠르게 회복하는 개미털기 포착"""
        if len(df) < lookback: return df
        
        df['Swing_Low'] = df['low'].rolling(window=lookback).min().shift(1)
        df['Bullish_Sweep'] = (df['low'] < df['Swing_Low']) & (df['close'] > df['Swing_Low'])
        return df

    @staticmethod
    def detect_order_block(df, impulse_threshold=0.015):
        """기관의 대량 매집 구간 (마지막 음봉) 포착"""
        df['OB_Active'] = False
        df['OB_High'] = np.nan
        df['OB_Low'] = np.nan
        
        df['is_Bearish'] = df['close'] < df['open']
        df['is_Bullish'] = df['close'] > df['open']
        df['Body_Size'] = abs(df['close'] - df['open']) / df['open']
        
        for i in range(1, len(df)):
            if df['is_Bullish'].iloc[i] and df['Body_Size'].iloc[i] > impulse_threshold:
                if df['is_Bearish'].iloc[i-1]:
                    df.at[df.index[i], 'OB_Active'] = True
                    df.at[df.index[i], 'OB_High'] = df['high'].iloc[i-1]
                    df.at[df.index[i], 'OB_Low'] = df['low'].iloc[i-1]
        return df

    @staticmethod
    def generate_smc_signal(df):
        """SMC 통합 신호: 유동성 사냥 후 오더블록 발생 시 매수"""
        df['SMC_Signal'] = 'NONE'
        df['Entry_Price'] = np.nan
        df['Stop_Loss'] = np.nan
        df['Target'] = np.nan
        
        for i in range(1, len(df)):
            if df['Bullish_Sweep'].iloc[i-1] and df['OB_Active'].iloc[i]:
                df.at[df.index[i], 'SMC_Signal'] = 'BUY'
                df.at[df.index[i], 'Entry_Price'] = df['OB_High'].iloc[i]
                df.at[df.index[i], 'Stop_Loss'] = df['OB_Low'].iloc[i] * 0.99
                df.at[df.index[i], 'Target'] = df['OB_High'].iloc[i] * 1.05
        return df
