"""
국내 주식 신호 분석 (기술적 분석 기반).
"""
import logging
import os
from typing import Dict

from core.investor_flow import InvestorFlow

logger = logging.getLogger(__name__)


class SignalAnalyzerKospi:
    """국내 주식 매수/매도 신호 분석"""

    # ── 점수 계산 ─────────────────────────────────────────────────────────

    def calculate_score(self, symbol: str, price_data: Dict, sector_bonus: float = 0.0) -> float:
        """
        종목 점수 계산 (0~100).

        배점:
        - 연속 매집   최대 20점  (기관/외인 수급 프록시)
        - SMA 정렬    20점
        - MACD        15점
        - BB 위치     15점
        - 거래량      15점
        - 5일 등락    15점
        - RSI         최대 10점  (보조 — 백테스트상 고가중 역효과)
        - 저PBR 밸류업 최대 10점  (선택)
        - 섹터 모멘텀  ±15%  (업종 분봉 기반 가중치)
        """
        try:
            score = 0.0

            # 연속 매집 신호 (기관/외인 수급 프록시) ─ Strategy 2
            cbd = price_data.get('consecutive_buy_days', 0)
            if cbd >= 5:
                score += 20
            elif cbd >= 3:
                score += 12
            elif cbd >= 1:
                score += 5

            # 저PBR 밸류업 보너스 (데이터 있을 때만)
            pbr = price_data.get('pbr', None)
            if pbr is not None:
                if 0 < pbr < 1.0:
                    score += 10
                elif 1.0 <= pbr < 1.5:
                    score += 5

            # RSI: 55~68 최적 구간 (백테스트상 과가중 시 역효과 확인 → 최대 10점으로 축소)
            rsi = price_data.get('rsi', 50)
            if 55 <= rsi <= 68:
                score += min((rsi - 50) * 0.55, 10)

            # MACD 골든크로스
            if price_data.get('macd', 0) > price_data.get('macd_signal', 0):
                score += 15

            # SMA 골든크로스 (20 > 60)
            if price_data.get('sma_20', 0) > price_data.get('sma_60', 0):
                score += 20

            # 볼린저 밴드 위치
            close     = price_data.get('close', 0)
            bb_upper  = price_data.get('bb_upper', 0)
            bb_middle = price_data.get('bb_middle', 0)
            if close > bb_middle and bb_upper > bb_middle:
                band_ratio = (close - bb_middle) / (bb_upper - bb_middle)
                score += min(band_ratio, 1.0) * 15

            # 거래량 (20일 평균 대비)
            volume     = price_data.get('volume', 0)
            avg_volume = price_data.get('avg_volume_20', volume)
            if avg_volume > 0:
                ratio = volume / avg_volume
                if ratio > 1.2:
                    score += min((ratio - 1) * 30, 15)

            # 5일 수익률
            prev5 = price_data.get('close_5d_ago', close)
            if prev5 > 0 and close > prev5:
                change_pct = (close - prev5) / prev5 * 100
                score += min(change_pct * 0.5, 15)

            # SMA224/448 돌파 보너스 — 역배열 탈출 후 강한 상승 전환 신호
            sma_224     = price_data.get('sma_224', 0)
            sma_224_prev = price_data.get('sma_224_prev', 0)
            close_prev  = price_data.get('close_prev', close)
            sma_448     = price_data.get('sma_448', 0)
            vol_surge   = avg_volume > 0 and volume >= avg_volume * 1.5
            if sma_224 > 0 and sma_224_prev > 0:
                if close > sma_224 and close_prev < sma_224_prev and vol_surge:
                    score += 25   # 224일선 골든크로스 + 거래량 급증
            if sma_448 > 0:
                if close > sma_448 and close_prev <= sma_448 and vol_surge:
                    score += 30   # 448일선 돌파 + 거래량 급증

            # 섹터 모멘텀 가중치 (±15%) — 강한 섹터 종목 부스트, 약한 섹터 억제
            if sector_bonus != 0.0:
                score = score * (1.0 + sector_bonus)

            return min(score, 100.0)

        except Exception as e:
            logger.error(f"점수 계산 오류 ({symbol}): {e}")
            return 0.0

    # ── 신호 감지 ────────────────────────────────────────────────────────

    def detect_signal(self, symbol: str, price_data: Dict) -> str:
        """
        매수/매도/보유 신호 반환.

        매도(SELL): 손절선 이탈 또는 RSI 과매도
        매수(BUY) : 종합 점수 + 추세 + 거래량 확인
        """
        try:
            rsi       = price_data.get('rsi', 50)
            close     = price_data.get('close', 0)
            sma_20    = price_data.get('sma_20', 0)
            sma_60    = price_data.get('sma_60', 0)
            stop_loss = price_data.get('stop_loss', 0)
            volume = price_data.get('volume', 0)
            avg_volume = price_data.get('avg_volume_20', volume)

            # 매도 우선
            if stop_loss > 0 and close < stop_loss:
                logger.debug(f"  {symbol}: 손절선 이탈 → SELL")
                return 'SELL'
            if rsi <= 30:
                logger.debug(f"  {symbol}: RSI 과매도 ({rsi:.0f}) → SELL")
                return 'SELL'

            # 거래대금 필터: 20일 평균 거래대금 50억 미만 잡주 제외
            min_turnover = float(os.getenv("MIN_DAILY_TURNOVER", "5000000000") or 5000000000)
            avg_turnover = close * avg_volume
            if avg_turnover < min_turnover:
                logger.debug(f"  {symbol}: 거래대금 부족 (₩{avg_turnover/1e8:.0f}억 < 50억) → HOLD")
                return 'HOLD'

            score = self.calculate_score(symbol, price_data)
            volume_ok = avg_volume > 0 and volume >= avg_volume * 0.8
            trend_ok = close > sma_20 and (sma_20 >= sma_60 or rsi >= 65)
            momentum_ok = rsi >= 52 and price_data.get('macd', 0) > price_data.get('macd_signal', 0)

            # 눌림목 진입: RSI 과열(>78) 제외
            not_overbought = rsi <= 78
            # BB 상단 돌파 구간 제외 (이미 상단 위에서 추격 방지)
            bb_upper = price_data.get('bb_upper', 0)
            not_bb_top = bb_upper <= 0 or close < bb_upper * 0.99
            # SMA20 대비 12% 이상 이격 제외 (강한 상승장 허용폭 확대)
            not_stretched = sma_20 <= 0 or close <= sma_20 * 1.12

            # 수급 강도에 따른 임계값 조정 (Strategy 2)
            # 연속 매집 ≥3일 + 눌림목 진입 조건이면 점수 기준 완화
            cbd = price_data.get('consecutive_buy_days', 0)
            pullback_ok = InvestorFlow.is_pullback_entry({
                'close': close,
                'sma_20': sma_20,
                'sma_5': price_data.get('sma_5', sma_20),
            })
            buy_threshold = 58 if (cbd >= 3 and pullback_ok) else 65

            if score >= buy_threshold and trend_ok and momentum_ok and volume_ok \
                    and not_overbought and not_bb_top and not_stretched:
                logger.debug(
                    f"  {symbol}: 점수 {score:.1f} + RSI {rsi:.0f} + 매집{cbd}일 + 눌림목={pullback_ok} → BUY"
                )
                return 'BUY'

            return 'HOLD'

        except Exception as e:
            logger.error(f"신호 감지 오류 ({symbol}): {e}")
            return 'HOLD'
