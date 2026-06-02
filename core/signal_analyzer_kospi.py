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

    V43_GRADE_MULTIPLIER = {
        'A': 1.0,
        'B': 0.5,
        'C': 0.3,
    }

    def _grade_v43(self, score: float) -> str | None:
        if score >= 85:
            return 'A'
        if score >= 75:
            return 'B'
        if score >= 65:
            return 'C'
        return None

    def _classify_v43(self, price_data: Dict, strong_market: bool, selective: bool) -> tuple[str | None, str]:
        close = float(price_data.get('close', 0) or 0)
        if close <= 0:
            return None, '가격없음'

        volume = float(price_data.get('volume', 0) or 0)
        avg_volume = float(price_data.get('avg_volume_20', volume) or volume)
        volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        high20 = float(price_data.get('high_20d', close) or close)
        sma20 = float(price_data.get('sma_20', close) or close)
        sma60 = float(price_data.get('sma_60', sma20) or sma20)
        rsi = float(price_data.get('rsi', 50) or 50)
        cbd = int(price_data.get('consecutive_buy_days', 0) or 0)
        close5 = float(price_data.get('close_5d_ago', close) or close)
        close20 = float(price_data.get('close_20d_ago', close) or close)
        return5 = (close - close5) / close5 if close5 > 0 else 0.0
        return20 = (close - close20) / close20 if close20 > 0 else 0.0
        price_data['_v43_return5'] = return5
        price_data['_v43_return20'] = return20

        if return5 > 0.25:
            return None, f'v4.3과열차단(5일 {return5*100:.1f}%)'
        if rsi >= 82:
            return None, f'v4.3과열차단(RSI {rsi:.0f})'
        if sma20 > 0 and close > sma20 * 1.20:
            return None, f'v4.3이격차단({close/sma20:.2f}x)'

        if high20 > 0 and close >= high20 * 0.995 and volume_ratio >= 1.5:
            return 'BREAKOUT', f'20일고점근접+거래량{volume_ratio:.1f}x'

        if (
            close > sma20
            and sma20 >= sma60 * 0.98
            and -0.06 <= return5 <= 0.08
            and -0.05 <= return20 <= 0.30
            and 42 <= rsi <= 70
            and volume_ratio >= 0.75
        ):
            return 'PULLBACK', f'초입/눌림회복(5일{return5*100:.1f}%,거래량{volume_ratio:.1f}x)'

        if (
            close > sma20
            and 0.00 <= return5 <= 0.10
            and return20 <= 0.35
            and 45 <= rsi <= 72
            and 0.8 <= volume_ratio <= 1.8
        ):
            return 'MOMENTUM', f'초입모멘텀(5일{return5*100:.1f}%,RSI{rsi:.0f})'

        if cbd >= 3 and close > sma20 and return20 >= 0.03:
            return 'MOMENTUM', f'연속매집{cbd}일+20일{return20*100:.1f}%'

        if close > sma20 > sma60 and return5 < -0.01 and 40 <= rsi <= 70:
            return 'PULLBACK', f'상승추세눌림(5일{return5*100:.1f}%,RSI{rsi:.0f})'

        if selective or strong_market:
            if cbd >= 3 and close > sma20 and volume_ratio >= 1.2:
                return 'MOMENTUM', f'시장강세매집{cbd}일+거래량{volume_ratio:.1f}x'

        return None, 'v4.3유효패턴없음'

    def _detect_signal_v43(
        self,
        symbol: str,
        price_data: Dict,
        strong_market: bool,
        selective: bool,
    ) -> tuple[str, str]:
        rsi = float(price_data.get('rsi', 50) or 50)
        close = float(price_data.get('close', 0) or 0)
        stop_loss = float(price_data.get('stop_loss', 0) or 0)
        volume = float(price_data.get('volume', 0) or 0)
        avg_volume = float(price_data.get('avg_volume_20', volume) or volume)

        if stop_loss > 0 and close < stop_loss:
            return 'SELL', '손절선이탈'
        if rsi <= 30:
            return 'SELL', f'RSI과매도({rsi:.0f})'

        min_turnover = float(os.getenv("MIN_DAILY_TURNOVER", "5000000000") or 5000000000)
        avg_turnover = close * avg_volume
        if avg_turnover < min_turnover:
            return 'HOLD', f'거래대금부족({avg_turnover/1e8:.0f}억<50억)'

        score = self.calculate_score(symbol, price_data)
        grade = self._grade_v43(score)
        if grade is None:
            return 'HOLD', f'v4.3점수부족({score:.1f}<65)'

        strategy, reason = self._classify_v43(price_data, strong_market, selective)
        if strategy is None:
            return 'HOLD', reason

        if strategy == 'PULLBACK' and grade == 'C':
            return 'HOLD', f'v4.3 C-PULLBACK 차단({score:.1f})'

        volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        if grade == 'B' and strategy in ('MOMENTUM', 'BREAKOUT') and not (strong_market or selective or volume_ratio >= 2.0):
            return 'HOLD', f'v4.3 B-{strategy} 시장/거래량 확인 부족'

        mult = self.V43_GRADE_MULTIPLIER[grade]
        price_data['_v43_strategy'] = strategy
        price_data['_v43_grade'] = grade
        price_data['_v43_size_multiplier'] = mult

        return (
            'BUY',
            f'v4.3 {grade}-{strategy} 점수{score:.1f} 비중x{mult:.1f} | {reason}',
        )

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

            # 5일 수익률: 급등 추격보다 초입 상승/눌림 회복을 우선한다.
            prev5 = price_data.get('close_5d_ago', close)
            change_pct = 0.0
            if prev5 > 0:
                change_pct = (close - prev5) / prev5 * 100
                if -4 <= change_pct < 0:
                    score += 6 + change_pct * 0.5
                elif 0 <= change_pct <= 8:
                    score += 6 + change_pct * 0.75
                elif 8 < change_pct <= 12:
                    score += 8
                elif change_pct > 15:
                    score -= min((change_pct - 15) * 0.8, 18)

            # 초입/눌림 회복 보너스: 너무 오른 종목 대신 막 살아나는 종목을 위로 올린다.
            sma_5 = price_data.get('sma_5', close)
            sma_20 = price_data.get('sma_20', close)
            sma_60 = price_data.get('sma_60', sma_20)
            macd = price_data.get('macd', 0)
            macd_signal = price_data.get('macd_signal', 0)
            volume = price_data.get('volume', 0)
            avg_volume = price_data.get('avg_volume_20', volume)
            volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
            if (
                close > sma_20
                and sma_20 >= sma_60 * 0.98
                and close <= sma_5 * 1.04
                and -6 <= change_pct <= 8
                and 42 <= rsi <= 70
                and volume_ratio >= 0.75
            ):
                score += 12
            if (
                close > sma_20
                and close <= sma_20 * 1.08
                and 0 <= change_pct <= 10
                and macd > macd_signal
                and 0.8 <= volume_ratio <= 1.8
            ):
                score += 8

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

    def detect_signal(
        self,
        symbol: str,
        price_data: Dict,
        strong_market: bool = False,
        selective: bool = False,
    ) -> tuple[str, str]:
        """
        매수/매도/보유 신호와 사유를 함께 반환.

        반환: (signal, reason_str)
          signal  — 'BUY' | 'SELL' | 'HOLD'
          reason  — BUY면 통과 요약, HOLD/SELL이면 탈락 사유

        시장 국면별 임계값 3단계:
          strong_market=True,  selective=False  → 강한 상승장  (완화)
          strong_market=True,  selective=True   → 선택적 허용  (중간)
          strong_market=False                   → 일반/중립    (기본)
        """
        try:
            if os.getenv("KOSPI_V43_LIVE_FILTER", "true").lower() == "true":
                return self._detect_signal_v43(symbol, price_data, strong_market, selective)

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
                return 'SELL', '손절선이탈'
            if rsi <= 30:
                logger.debug(f"  {symbol}: RSI 과매도 ({rsi:.0f}) → SELL")
                return 'SELL', f'RSI과매도({rsi:.0f})'

            # 거래대금 필터: 20일 평균 거래대금 50억 미만 잡주 제외
            min_turnover = float(os.getenv("MIN_DAILY_TURNOVER", "5000000000") or 5000000000)
            avg_turnover = close * avg_volume
            if avg_turnover < min_turnover:
                logger.debug(f"  {symbol}: 거래대금 부족 (₩{avg_turnover/1e8:.0f}억 < 50억) → HOLD")
                return 'HOLD', f'거래대금부족({avg_turnover/1e8:.0f}억<50억)'

            # 시장 국면별 임계값 동적 조정
            if strong_market and not selective:
                # 강한 상승장 (KOSPI 강세 + 변동성 정상): 완화
                score_base    = float(os.getenv("SIGNAL_STRONG_SCORE",   "60")   or 60)
                rsi_floor     = float(os.getenv("SIGNAL_STRONG_RSI",     "48")   or 48)
                stretch_limit = float(os.getenv("STRONG_TREND_STRETCH",  "1.20") or 1.20)
                rsi_max       = float(os.getenv("STRONG_TREND_RSI_MAX",  "82")   or 82)
                bb_ratio      = float(os.getenv("STRONG_TREND_BB_RATIO", "1.03") or 1.03)
                mode_label = "강세장"
            elif strong_market and selective:
                # 선택적 허용 (KOSPI 강세 + 변동성 높음): 중간 완화
                score_base    = float(os.getenv("SIGNAL_SELECTIVE_SCORE",  "62")   or 62)
                rsi_floor     = float(os.getenv("SIGNAL_SELECTIVE_RSI",    "50")   or 50)
                stretch_limit = float(os.getenv("SELECTIVE_STRETCH",       "1.18") or 1.18)
                rsi_max       = float(os.getenv("SELECTIVE_RSI_MAX",       "82")   or 82)
                bb_ratio      = float(os.getenv("SELECTIVE_BB_RATIO",      "1.03") or 1.03)
                mode_label = "선택적"
            else:
                # 일반/중립
                score_base    = float(os.getenv("SIGNAL_NORMAL_SCORE",   "65")   or 65)
                rsi_floor     = float(os.getenv("SIGNAL_NORMAL_RSI",     "52")   or 52)
                stretch_limit = float(os.getenv("NORMAL_STRETCH",        "1.12") or 1.12)
                rsi_max       = float(os.getenv("NORMAL_RSI_MAX",        "78")   or 78)
                bb_ratio      = float(os.getenv("NORMAL_BB_RATIO",       "0.99") or 0.99)
                mode_label = "일반"

            score = self.calculate_score(symbol, price_data)
            volume_ok = avg_volume > 0 and volume >= avg_volume * 0.8
            trend_ok = close > sma_20 and (sma_20 >= sma_60 or rsi >= 65)
            momentum_ok = rsi >= rsi_floor and price_data.get('macd', 0) > price_data.get('macd_signal', 0)

            # RSI 과열 상한 (시장 국면별 완화)
            not_overbought = rsi <= rsi_max
            # BB 상단 허용 배율 (강세장에서 BB 상단 약간 초과 진입 허용)
            bb_upper = price_data.get('bb_upper', 0)
            not_bb_top = bb_upper <= 0 or close < bb_upper * bb_ratio
            # SMA20 대비 이격 한도
            not_stretched = sma_20 <= 0 or close <= sma_20 * stretch_limit

            # 수급 강도에 따른 임계값 조정: 연속 매집 ≥3일 + 눌림목이면 7pt 완화
            cbd = price_data.get('consecutive_buy_days', 0)
            pullback_ok = InvestorFlow.is_pullback_entry({
                'close': close,
                'sma_20': sma_20,
                'sma_5': price_data.get('sma_5', sma_20),
            })
            buy_threshold = (score_base - 7) if (cbd >= 3 and pullback_ok) else score_base

            # 당일 방향 필터: 하락봉(시가 대비 종가 -1% 이하)이면 3pt 가중
            open_price = price_data.get('open', close)
            day_chg_pct = (close - open_price) / open_price * 100 if open_price > 0 else 0
            _day_drop_penalty = float(os.getenv("SIGNAL_DAY_DROP_PENALTY", "3") or 3)
            _day_drop_thresh  = float(os.getenv("SIGNAL_DAY_DROP_THRESH",  "1.0") or 1.0)
            if day_chg_pct < -_day_drop_thresh:
                buy_threshold += _day_drop_penalty

            if score >= buy_threshold and trend_ok and momentum_ok and volume_ok \
                    and not_overbought and not_bb_top and not_stretched:
                buy_reason = (
                    f"점수{score:.1f} RSI{rsi:.0f} 매집{cbd}일 [{mode_label}]"
                )
                logger.debug(
                    f"  {symbol}: [{mode_label}] 점수 {score:.1f}>={buy_threshold} "
                    f"RSI {rsi:.0f}>={rsi_floor:.0f} 매집{cbd}일 눌림목={pullback_ok} → BUY"
                )
                return 'BUY', buy_reason

            _fail = []
            if score < buy_threshold:  _fail.append(f"점수{score:.1f}<{buy_threshold:.0f}({mode_label})")
            if not trend_ok:           _fail.append("추세미충족")
            if not momentum_ok:
                if rsi < rsi_floor:    _fail.append(f"RSI부족({rsi:.0f}<{rsi_floor:.0f})")
                else:                  _fail.append("MACD미충족")
            if not volume_ok:          _fail.append("거래량부족")
            if not not_overbought:     _fail.append(f"RSI과매수({rsi:.0f}>{rsi_max:.0f})")
            if not not_bb_top:         _fail.append("BB상단초과")
            if not not_stretched:      _fail.append(f"이격초과({close/sma_20:.2f}x)" if sma_20 > 0 else "이격초과")
            hold_reason = ','.join(_fail) or '조건미충족'
            logger.debug(f"  {symbol}: HOLD — {hold_reason}")
            return 'HOLD', hold_reason

        except Exception as e:
            logger.error(f"신호 감지 오류 ({symbol}): {e}")
            return 'HOLD', f'오류({e})'
