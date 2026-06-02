"""매수/매도 신호 엔진 — v1/v2 점수 로직 유지 + 조건 단순화

매수: score ≥ threshold (regime별) AND 5일선 위 AND 거래량 OK
매도: 손절 / 트레일링 / 익절 / RSI과매도
"""
from __future__ import annotations

import logging
import os

from core.regime import MarketSnapshot, Regime

logger = logging.getLogger(__name__)


class SignalEngine:

    # ── 점수 계산 ─────────────────────────────────────────────────────────

    def score(self, symbol: str, price_data: dict, sector_bonus: float = 0.0) -> float:
        """종목 점수 0~100. v1/v2와 동일한 배점 (검증된 로직 유지)."""
        try:
            s = 0.0

            # 연속 매집일 (기관/외인 수급 프록시) — 최대 20점
            cbd = price_data.get("consecutive_buy_days", 0)
            if cbd >= 5:   s += 20
            elif cbd >= 3: s += 12
            elif cbd >= 1: s += 5

            # 저PBR 밸류업 — 최대 10점
            pbr = price_data.get("pbr")
            if pbr:
                if 0 < pbr < 1.0:  s += 10
                elif pbr < 1.5:    s += 5

            # RSI 55~68 최적 구간 — 최대 10점
            rsi = price_data.get("rsi", 50)
            if 55 <= rsi <= 68:
                s += min((rsi - 50) * 0.55, 10)

            # MACD 골든크로스 — 15점
            if price_data.get("macd", 0) > price_data.get("macd_signal", 0):
                s += 15

            # SMA20 > SMA60 골든크로스 — 20점
            if price_data.get("sma_20", 0) > price_data.get("sma_60", 0):
                s += 20

            # 볼린저밴드 위치 — 최대 15점
            close     = price_data.get("close", 0)
            bb_upper  = price_data.get("bb_upper", 0)
            bb_middle = price_data.get("bb_middle", 0)
            if close > bb_middle and bb_upper > bb_middle:
                s += min((close - bb_middle) / (bb_upper - bb_middle), 1.0) * 15

            # 거래량 급증 (20일 평균 대비) — 최대 15점
            volume     = price_data.get("volume", 0)
            avg_volume = price_data.get("avg_volume_20", volume)
            if avg_volume > 0 and volume > avg_volume * 1.2:
                s += min((volume / avg_volume - 1) * 30, 15)

            # 5일 수익률 — 최대 15점
            prev5 = price_data.get("close_5d_ago", close)
            if prev5 > 0 and close > prev5:
                s += min((close - prev5) / prev5 * 100 * 0.5, 15)

            # 224일선/448일선 돌파 보너스 (주식단테 대시세 조건)
            sma_224      = price_data.get("sma_224", 0)
            sma_224_prev = price_data.get("sma_224_prev", 0)
            close_prev   = price_data.get("close_prev", close)
            sma_448      = price_data.get("sma_448", 0)
            vol_surge    = avg_volume > 0 and volume >= avg_volume * 1.5
            if sma_224 > 0 and sma_224_prev > 0:
                if close > sma_224 and close_prev < sma_224_prev and vol_surge:
                    s += 13   # 224일선 돌파 보너스 (보조 추세확인, 단독 A승격 방지)
            if sma_448 > 0:
                if close > sma_448 and close_prev <= sma_448 and vol_surge:
                    s += 30   # 448일선 돌파 + 거래량 급증 (대시세 핵심)

            if sector_bonus:
                s = s * (1.0 + sector_bonus)

            return min(s, 100.0)

        except Exception as e:
            logger.error("점수 계산 오류 (%s): %s", symbol, e)
            return 0.0

    # ── 매수 신호 ─────────────────────────────────────────────────────────

    def should_buy(
        self,
        symbol: str,
        price_data: dict,
        snap: MarketSnapshot,
    ) -> tuple[bool, str]:
        """
        매수 여부. (True, 이유) or (False, 탈락사유).

        핵심 조건 3개 (AND):
          1) score ≥ threshold  (regime별 조정)
          2) 5일선 위           (단기 추세 확인)
          3) 거래량 ≥ 평균 80%  (잡주/거래 정지 방어)
        """
        try:
            close      = price_data.get("close", 0)
            sma_5      = price_data.get("sma_5", 0)
            sma_20     = price_data.get("sma_20", 0)
            volume     = price_data.get("volume", 0)
            avg_volume = price_data.get("avg_volume_20", volume)
            rsi        = price_data.get("rsi", 50)

            # 거래대금 필터 (잡주 제거)
            min_turnover = float(os.getenv("MIN_DAILY_TURNOVER", "5000000000"))
            if avg_volume > 0 and close * avg_volume < min_turnover:
                return False, f"거래대금 부족 ({close * avg_volume / 1e8:.0f}억)"

            sc        = self.score(symbol, price_data)
            base      = float(os.getenv("SIGNAL_NORMAL_SCORE", "58"))
            threshold = snap.score_threshold(base)

            if sc < threshold:
                return False, f"점수 {sc:.1f} < {threshold:.0f} ({snap.regime.value})"

            # 5일선 위 (sma_5 없으면 sma_20으로 대체)
            ref_ma = sma_5 if sma_5 > 0 else sma_20
            if ref_ma > 0 and close < ref_ma * 0.99:
                return False, f"5일선 아래 (₩{close:,.0f} < ₩{ref_ma:,.0f})"

            # 거래량
            if avg_volume > 0 and volume < avg_volume * 0.8:
                return False, f"거래량 부족 ({volume / avg_volume:.2f}x 평균)"

            # RSI 과열 차단
            if rsi >= 82:
                return False, f"RSI 과열 ({rsi:.0f})"

            # 5일 급등 차단 (+12% 이상)
            prev5 = price_data.get("close_5d_ago", close)
            if prev5 and prev5 > 0 and (close - prev5) / prev5 * 100 >= 12.0:
                return False, f"5일 급등 ({(close - prev5) / prev5 * 100:.1f}%)"

            # SMA20 과도 이격 차단 (+12% 초과)
            if sma_20 > 0 and close > sma_20 * 1.12:
                return False, f"SMA20 이격 과대 ({(close / sma_20 - 1) * 100:.1f}%)"

            # MA20 기울기 ≤ 0 차단
            sma_20_prev = price_data.get("sma_20_prev", 0)
            if sma_20 > 0 and sma_20_prev and sma_20_prev > 0 and sma_20 <= sma_20_prev:
                return False, "MA20 하락/횡보 구간"

            cbd    = price_data.get("consecutive_buy_days", 0)
            reason = (
                f"점수 {sc:.1f} | RSI {rsi:.0f} | 매집 {cbd}일 | {snap.regime.value}"
            )
            return True, reason

        except Exception as e:
            logger.error("매수 판단 오류 (%s): %s", symbol, e)
            return False, f"오류: {e}"

    # ── 매도 신호 ─────────────────────────────────────────────────────────

    def should_sell(
        self,
        symbol: str,
        price_data: dict,
        buy_price: float,
        highest_price: float,
    ) -> tuple[bool, str]:
        """손절 / 트레일링 / 익절 / RSI과매도 판단."""
        try:
            close  = price_data.get("close", buy_price)
            rsi    = price_data.get("rsi", 50)
            profit = (close - buy_price) / buy_price * 100 if buy_price else 0

            stop_loss_pct    = float(os.getenv("STOP_LOSS_PCT",    "-3.0"))
            trailing_gap_pct = float(os.getenv("TRAILING_GAP_PCT", "5.0"))
            take_profit_pct  = float(os.getenv("TAKE_PROFIT_PCT",  "15.0"))

            if profit <= stop_loss_pct:
                return True, f"손절 {profit:.1f}%"

            if profit >= take_profit_pct:
                return True, f"익절 {profit:.1f}%"

            if highest_price > buy_price:
                trail_price = highest_price * (1 - trailing_gap_pct / 100)
                if close < trail_price:
                    max_profit = (highest_price - buy_price) / buy_price * 100
                    return True, f"트레일링 (최고 +{max_profit:.1f}% → 현 {profit:.1f}%)"

            if rsi <= 30:
                return True, f"RSI 과매도 ({rsi:.0f})"

            return False, ""

        except Exception as e:
            logger.error("매도 판단 오류 (%s): %s", symbol, e)
            return False, ""
