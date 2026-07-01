import pandas as pd
import numpy as np

class RiskManagement:
    """리스크 관리 및 동적 손절 (ATR 기반)"""
    
    @staticmethod
    def calculate_atr(df, period=14):
        df['TR'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                abs(df['high'] - df['close'].shift()),
                abs(df['low'] - df['close'].shift())
            )
        )
        df['ATR'] = df['TR'].rolling(period).mean()
        return df

    @staticmethod
    def calculate_position_size(account_balance, risk_pct, entry_price, stop_loss):
        """
        Position Size = (Account × Risk%) / (Entry - Stop Loss)
        """
        risk_amount = account_balance * risk_pct
        price_distance = abs(entry_price - stop_loss)
        
        if price_distance == 0: return 0
        
        position_size = int(risk_amount / price_distance)
        return position_size

    @staticmethod
    def trailing_stop(current_price, highest_price, atr, multiplier=2):
        """최고가 대비 ATR*배수 만큼 뒤따라가는 손절선.

        하한은 최고가의 92% (최고가 기준 -8%)로 설정.
        current_price 기준 하한을 쓰면 정상 등락에 조기 손절되므로 제거.
        """
        if pd.isna(atr) or atr <= 0:
            return highest_price * 0.95  # ATR 없으면 최고가 -5%

        ts_price = highest_price - (atr * multiplier)
        # 너무 넓은 스톱 방지: 최고가의 95% 이상 보장 (5% 슬랙)
        return max(ts_price, highest_price * 0.95)
