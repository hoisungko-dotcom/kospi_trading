"""
외국인/기관 연속 순매수 추종 전략.
- 실거래: KIS API flow_list (foreigner_net, institution_net, net_flow 포함)
- 백테스트: 가격/거래량 기반 매집 프록시 사용
"""
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class InvestorFlow:

    # ── 연속 순매수 일수 계산 ───────────────────────────────────────────────

    @staticmethod
    def consecutive_net_buy_count(flow_list: list, who: str = 'both') -> int:
        """
        최근부터 역산하여 연속 순매수 일수 반환.
        who: 'both'(외인+기관 합산) | 'foreigner' | 'institution'
        flow_list: [{'foreigner_net':int, 'institution_net':int, 'net_flow':int}, ...]
                   최신 데이터가 마지막([-1]) 인덱스에 위치한다고 가정.
        """
        if not flow_list:
            return 0
        count = 0
        for f in reversed(flow_list):
            if who == 'foreigner':
                net = f.get('foreigner_net', 0)
            elif who == 'institution':
                net = f.get('institution_net', 0)
            else:
                net = f.get('net_flow', 0)
            if net > 0:
                count += 1
            else:
                break
        return count

    @staticmethod
    def buy_pressure_days(price_history: list) -> int:
        """
        가격/거래량 기반 매집 프록시 (백테스트·KIS 미연결 시).
        조건: 양봉(close>open) AND 종가가 당일 레인지 상위 40% AND 거래량 >= 20일 평균
        """
        if len(price_history) < 5:
            return 0

        closes  = [d['close']  for d in price_history]
        opens   = [d['open']   for d in price_history]
        highs   = [d['high']   for d in price_history]
        lows    = [d['low']    for d in price_history]
        volumes = [d['volume'] for d in price_history]

        avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)

        count = 0
        for i in range(len(price_history) - 1, max(len(price_history) - 8, -1), -1):
            c, o, h, l, v = closes[i], opens[i], highs[i], lows[i], volumes[i]
            rng = h - l
            bullish_body    = c > o
            upper_close     = rng > 0 and (c - l) / rng >= 0.60  # 레인지 상위 40%
            volume_ok       = avg_vol > 0 and v >= avg_vol * 0.9
            if bullish_body and upper_close and volume_ok:
                count += 1
            else:
                break
        return count

    # ── 점수 계산 ──────────────────────────────────────────────────────────

    @staticmethod
    def institutional_flow_score(flow_list: list = None,
                                  price_history: list = None,
                                  pbr: float = None) -> float:
        """
        기관/외인 연속 순매수 점수 (0~1).
        flow_list 있으면 실거래 데이터 우선 사용, 없으면 price_history 프록시.
        pbr: 저PBR 밸류업 프로그램 보너스.
        """
        # 연속 매수 일수
        if flow_list:
            days = InvestorFlow.consecutive_net_buy_count(flow_list)
        elif price_history:
            days = InvestorFlow.buy_pressure_days(price_history)
        else:
            return 0.5  # 데이터 없음 → 중립

        # 일수 → 기본 점수
        if days >= 5:
            base = 1.0
        elif days >= 3:
            base = 0.82
        elif days >= 2:
            base = 0.65
        elif days >= 1:
            base = 0.50
        else:
            base = 0.15  # 순매도 또는 관망

        # 저PBR 보너스 (밸류업 프로그램 수혜 가능성)
        pbr_bonus = 0.0
        if pbr is not None and 0 < pbr < 1.0:
            pbr_bonus = 0.12
        elif pbr is not None and 1.0 <= pbr < 1.5:
            pbr_bonus = 0.06

        return min(1.0, base + pbr_bonus)

    # ── 눌림목 진입 판단 ───────────────────────────────────────────────────

    @staticmethod
    def is_pullback_entry(price_data: dict) -> bool:
        """
        수급 유입 후 눌림목 진입 조건.
        - 현재가가 SMA20 위 (추세 유지)
        - SMA20 대비 이격이 8% 이내 (너무 멀리 안 올라감)
        - SMA5 >= SMA20 (단기 추세 정배열)
        """
        close = price_data.get('close', 0)
        sma20 = price_data.get('sma_20', 0)
        sma5  = price_data.get('sma_5', 0)
        if sma20 <= 0 or close <= 0:
            return False
        above_sma20   = close >= sma20
        not_stretched = close <= sma20 * 1.08
        sma_aligned   = sma5 >= sma20 * 0.99  # SMA5 ≈ SMA20 이상
        return above_sma20 and not_stretched and sma_aligned

    # ── 하위 호환 (기존 코드에서 참조 시) ────────────────────────────────

    @staticmethod
    def detect_flow_reversal(symbol, flow_list, lookback=5):
        """[deprecated] 반전 감지. 신규 코드는 consecutive_net_buy_count 사용."""
        if len(flow_list) < lookback:
            return 'INSUFFICIENT_DATA', 0
        flows = [f['net_flow'] for f in flow_list]
        if flows[0] < 0 and flows[-1] > 0:
            strength = flows[-1] / abs(flows[0]) if flows[0] != 0 else 0
            return ('STRONG_REVERSAL', strength) if strength > 2 else ('WEAK_REVERSAL', strength)
        return 'NO_REVERSAL', 0

    @staticmethod
    def flow_momentum_score(flow_list):
        """[deprecated] 기존 통합 신호 호환용."""
        if not flow_list:
            return 0.5
        days = InvestorFlow.consecutive_net_buy_count(flow_list)
        return InvestorFlow.institutional_flow_score(flow_list=flow_list)
