import logging

logger = logging.getLogger(__name__)


class DynamicExitAnalyzerKospi:
    """국내 주식 동적 익절/손절 분석"""

    def assess_trend_strength(self, symbol: str, price_data: dict) -> int:
        """
        추세 강도 평가 (0~100)

        지표:
        - SMA 정렬 (40점)
        - RSI (20점)
        - 거래량 (20점)
        - MACD (10점)
        - 가격 위치 (10점)
        """
        trend_score = 0

        close = price_data.get('close', 0)
        sma_5 = price_data.get('sma_5', close)
        sma_20 = price_data.get('sma_20', close)
        sma_60 = price_data.get('sma_60', close)

        if sma_5 > sma_20 > sma_60:
            trend_score += 40
            logger.debug(f"  {symbol}: SMA 완벽 정렬 (+40)")
        elif sma_5 > sma_20:
            trend_score += 25
            logger.debug(f"  {symbol}: SMA 부분 정렬 (+25)")
        elif sma_5 > sma_60:
            trend_score += 10

        rsi = price_data.get('rsi', 50)
        if rsi > 70:
            trend_score += 20
            logger.debug(f"  {symbol}: RSI > 70 (+20)")
        elif rsi > 60:
            trend_score += 15
        elif rsi > 50:
            trend_score += 5

        volume = price_data.get('volume', 0)
        avg_volume = price_data.get('avg_volume_20', volume)

        if avg_volume > 0:
            volume_ratio = volume / avg_volume
            if volume_ratio > 1.5:
                trend_score += 20
                logger.debug(f"  {symbol}: 거래량 ↑↑ (+20)")
            elif volume_ratio > 1.2:
                trend_score += 12
            elif volume_ratio > 0.9:
                trend_score += 5

        macd = price_data.get('macd', 0)
        macd_signal = price_data.get('macd_signal', 0)

        if macd > macd_signal:
            trend_score += 10
            logger.debug(f"  {symbol}: MACD 골든크로스 (+10)")

        low_52w = price_data.get('low_52w', close)
        if low_52w > 0:
            price_from_low = ((close - low_52w) / low_52w) * 100
            if price_from_low > 50:
                trend_score += 10

        return min(trend_score, 100)

    def calculate_dynamic_take_profit(self, symbol: str, buy_price: float,
                                      current_price: float, trend_score: int, atr: float = 0) -> tuple:
        """
        동적 익절 가격 및 조건 계산 (Volatility-Aware)
        
        atr: 현재 ATR (0이면 무시)
        반환: (should_sell, reason, profit_target_pct)
        """
        current_profit = ((current_price - buy_price) / buy_price) * 100
        
        # 변동성 기반 목표 (예: 매수가 + 3 * ATR)
        atr_profit_target = ((atr * 3.0) / buy_price * 100) if atr > 0 else 10.0
        # 최소 5%, 최대 25% 제한
        atr_profit_target = max(5.0, min(25.0, atr_profit_target))

        logger.info(f"\n  [{symbol}] 익절 판단:")
        logger.info(f"    현재 수익: {current_profit:.2f}% | ATR 기반 목표: {atr_profit_target:.1f}%")
        logger.info(f"    추세 강도: {trend_score}/100")

        if trend_score >= 80:
            # 강한 추세에서는 목표가 도달해도 '추세 꺾임'이 나올 때까지 홀딩 (Trailing Stop에 맡김)
            if current_profit >= atr_profit_target:
                logger.info(f"    → 강한 추세 + 목표({atr_profit_target:.1f}%) 돌파! → 추세 꺾임 감지 대기")
            return False, "STRONG_TREND_HOLD", atr_profit_target

        elif trend_score >= 50:
            # 보통 추세: ATR 목표 또는 10% 중 큰 것
            target = max(10.0, atr_profit_target)
            if current_profit >= target:
                logger.warning(f"    → 보통 추세 + 목표({target:.1f}%) 달성 = 익절!")
                return True, f"NORMAL_TREND_{target:.0f}PCT", target
            return False, "NORMAL_TREND_WAIT", target

        elif trend_score >= 20:
            # 약한 추세: ATR 목표 또는 5% 중 작은 것 (보수적)
            target = min(7.0, max(5.0, atr_profit_target))
            if current_profit >= target:
                logger.warning(f"    → 약한 추세 + 목표({target:.1f}%) 달성 = 익절!")
                return True, f"WEAK_TREND_{target:.0f}PCT", target
            return False, "WEAK_TREND_WAIT", target

        else:
            # 횡보: 3% 또는 1 * ATR
            target = max(3.0, (atr / buy_price * 100) if atr > 0 else 3.0)
            target = min(5.0, target)
            if current_profit >= target:
                logger.warning(f"    → 횡보 + 목표({target:.1f}%) 달성 = 즉시 익절!")
                return True, f"NO_TREND_{target:.0f}PCT", target
            return False, "NO_TREND_WAIT", target

    def detect_trend_breakage(self, symbol: str, price_data: dict,
                              holding: dict, profit: float) -> tuple:
        """
        강한 추세에서 추세 꺾임 감지

        반환: (is_breakage, reason)
        """
        if profit < 5:
            return False, None

        signals = []

        rsi = price_data.get('rsi', 50)
        rsi_prev = price_data.get('rsi_prev', rsi)

        if rsi > 70 and rsi < rsi_prev:
            signals.append("RSI 70+ 하락")
            logger.warning(f"    ⚡ 신호 1: RSI {rsi:.0f} ↓")

        macd = price_data.get('macd', 0)
        macd_signal = price_data.get('macd_signal', 0)
        macd_prev = price_data.get('macd_prev', macd)
        macd_signal_prev = price_data.get('macd_signal_prev', macd_signal)

        if macd < macd_signal and macd_prev > macd_signal_prev:
            signals.append("MACD 데드크로스")
            logger.warning(f"    ⚡ 신호 2: MACD 데드크로스")

        close = price_data.get('close', 0)
        sma_5 = price_data.get('sma_5', close)
        sma_20 = price_data.get('sma_20', close)

        if sma_5 < sma_20:
            signals.append("SMA 정렬 깨짐")
            logger.warning(f"    ⚡ 신호 3: SMA5({sma_5:.0f}) < SMA20({sma_20:.0f})")

        volume = price_data.get('volume', 0)
        avg_volume = price_data.get('avg_volume_20', volume)

        if avg_volume > 0:
            volume_ratio = volume / avg_volume
            if volume_ratio < 0.6:
                signals.append("거래량 급감")
                logger.warning(f"    ⚡ 신호 4: 거래량 {volume_ratio * 100:.0f}%")

        resistance = holding.get('resistance', 0)
        if resistance > 0 and close > resistance:
            if close < price_data.get('high_20d', close) * 0.98:
                signals.append("저항선 돌파 실패")
                logger.warning(f"    ⚡ 신호 5: 저항선({resistance}) 돌파 실패")

        if len(signals) >= 2:
            reason = " + ".join(signals)
            logger.warning(f"  ⚠️ 추세 꺾임 감지: {reason}")
            return True, reason

        return False, None
