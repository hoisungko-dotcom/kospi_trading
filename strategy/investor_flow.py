"""
외국인/기관 연속 순매수 추종 전략.
- 실거래: 브로커 flow_list (foreigner_net, institution_net, net_flow 포함)
- 백테스트: 가격/거래량 기반 매집 프록시 사용
"""
import numpy as np


class InvestorFlow:
    @staticmethod
    def consecutive_net_buy_count(flow_list: list, who: str = 'both') -> int:
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
        if len(price_history) < 5:
            return 0
        closes = [d['close'] for d in price_history]
        opens = [d['open'] for d in price_history]
        highs = [d['high'] for d in price_history]
        lows = [d['low'] for d in price_history]
        volumes = [d['volume'] for d in price_history]
        avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        count = 0
        for i in range(len(price_history) - 1, max(len(price_history) - 8, -1), -1):
            c, o, h, l, v = closes[i], opens[i], highs[i], lows[i], volumes[i]
            rng = h - l
            bullish_body = c > o
            upper_close = rng > 0 and (c - l) / rng >= 0.60
            volume_ok = avg_vol > 0 and v >= avg_vol * 0.9
            if bullish_body and upper_close and volume_ok:
                count += 1
            else:
                break
        return count

    @staticmethod
    def institutional_flow_score(flow_list: list = None, price_history: list = None, pbr: float = None) -> float:
        if flow_list:
            days = InvestorFlow.consecutive_net_buy_count(flow_list)
        elif price_history:
            days = InvestorFlow.buy_pressure_days(price_history)
        else:
            return 0.5
        if days >= 5:
            base = 1.0
        elif days >= 3:
            base = 0.82
        elif days >= 2:
            base = 0.65
        elif days >= 1:
            base = 0.50
        else:
            base = 0.15
        pbr_bonus = 0.0
        if pbr is not None and 0 < pbr < 1.0:
            pbr_bonus = 0.12
        elif pbr is not None and 1.0 <= pbr < 1.5:
            pbr_bonus = 0.06
        return min(1.0, base + pbr_bonus)

    @staticmethod
    def is_pullback_entry(price_data: dict) -> bool:
        close = price_data.get('close', 0)
        sma20 = price_data.get('sma_20', 0)
        sma5 = price_data.get('sma_5', 0)
        if sma20 <= 0 or close <= 0:
            return False
        above_sma20 = close >= sma20
        not_stretched = close <= sma20 * 1.08
        sma_aligned = sma5 >= sma20 * 0.99
        return above_sma20 and not_stretched and sma_aligned

    @staticmethod
    def detect_flow_reversal(symbol, flow_list, lookback=5):
        if len(flow_list) < lookback:
            return 'INSUFFICIENT_DATA', 0
        flows = [f['net_flow'] for f in flow_list]
        if flows[0] < 0 and flows[-1] > 0:
            strength = flows[-1] / abs(flows[0]) if flows[0] != 0 else 0
            return ('STRONG_REVERSAL', strength) if strength > 2 else ('WEAK_REVERSAL', strength)
        return 'NO_REVERSAL', 0

    @staticmethod
    def flow_momentum_score(flow_list):
        if not flow_list:
            return 0.5
        return InvestorFlow.institutional_flow_score(flow_list=flow_list)
