import pandas as pd
import numpy as np
import logging

from core.smc_strategy import SMCStrategy
from core.ichimoku_strategy import IchimokuStrategy
from core.investor_flow import InvestorFlow
from core.volume_candle import VolumeCandleStrategy
from core.volatility_analysis import VolatilityAnalysis

logger = logging.getLogger(__name__)

class IntegratedSignal:
    """최강 전략 완전판: 가중치 기반 신호 통합"""
    
    def calculate_combined_score(self, symbol, df_daily, df_5min, investor_flow_list):
        """
        최종 신뢰도 가중치 배분:
          1. SMC (유동성 사냥 + 오더블록): 35%
          2. 외국인/기관 순매수: 25%
          3. 일목균형표: 15%
          4. 다중 타임프레임 확인: 15%
          5. 거래량 + 캔들 패턴: 10%
        """
        
        scores = {}
        
        # 1. SMC 신호 (35%)
        df_daily = SMCStrategy.detect_liquidity_sweep(df_daily)
        df_daily = SMCStrategy.detect_order_block(df_daily)
        df_daily = SMCStrategy.generate_smc_signal(df_daily)
        
        smc_score = 0
        if df_daily['SMC_Signal'].iloc[-1] == 'BUY':
            smc_score = 1.0
        elif df_daily['Bullish_Sweep'].iloc[-1] or df_daily['OB_Active'].iloc[-1]:
            smc_score = 0.5
        scores['SMC'] = smc_score
        
        # 2. 외국인/기관 수급 (25%)
        # 최근 5일 데이터를 바탕으로 모멘텀 점수 계산
        scores['Flow'] = InvestorFlow.flow_momentum_score(investor_flow_list)
        
        # 3. 일목균형표 (15%)
        df_daily = IchimokuStrategy.calculate_ichimoku(df_daily)
        df_daily = IchimokuStrategy.generate_ichimoku_signal(df_daily)
        
        ichimoku_sig = df_daily['Ichimoku_Signal'].iloc[-1]
        scores['Ichimoku'] = 1.0 if ichimoku_sig == 'STRONG_BUY' else 0.7 if ichimoku_sig == 'BUY' else 0.3
        
        # 4. 다중 타임프레임 확인 (15%)
        # 일봉 신호가 있고 5분봉에서 양봉/거래량 확인 시 가점
        mtf_score = 0.5 # 기본
        if smc_score > 0 and df_5min is not None and not df_5min.empty:
            if df_5min['close'].iloc[-1] > df_5min['open'].iloc[-1]:
                mtf_score = 1.0
        scores['MTF'] = mtf_score
        
        # 5. 거래량 + 캔들 패턴 (10%)
        df_daily = VolumeCandleStrategy.detect_volume_spike(df_daily)
        df_daily = VolumeCandleStrategy.detect_hammer(df_daily)
        df_daily = VolumeCandleStrategy.detect_engulfing(df_daily)
        scores['VolCandle'] = VolumeCandleStrategy.get_combined_score(df_daily)
        
        # 최종 점수 합산
        final_score = (
            scores['SMC'] * 0.35 +
            scores['Flow'] * 0.25 +
            scores['Ichimoku'] * 0.15 +
            scores['MTF'] * 0.15 +
            scores['VolCandle'] * 0.10
        )
        
        return final_score, scores

    def get_recommendation(self, final_score):
        """진입 기준 가이드 적용"""
        if final_score >= 0.75: return 'STRONG_BUY', final_score
        elif final_score >= 0.65: return 'BUY', final_score
        elif final_score >= 0.55: return 'WEAK_BUY', final_score
        return 'PASS', final_score
