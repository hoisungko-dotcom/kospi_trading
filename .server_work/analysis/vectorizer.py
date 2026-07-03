"""
급등 직전 캔들 → 25차원 벡터 변환

벡터 구성 (캔들 1개 = 5차원, 5캔들 = 25차원):
  [0] 캔들 방향: +1(양봉) / 0(도지) / -1(음봉)
  [1] 몸통크기 / ATR
  [2] 윗꼬리   / ATR
  [3] 아랫꼬리 / ATR
  [4] 거래량비율 (해당 캔들 / 5봉 평균)
"""
from __future__ import annotations

import numpy as np
from typing import Sequence

from collector.surge_detector import Candle


def _atr5(candles: Sequence[Candle]) -> float:
    trs = [max(c.high - c.low, 1e-4) for c in candles]
    return float(np.mean(trs)) or 1e-4


def candles_to_vector(pre_candles: list[Candle]) -> np.ndarray:
    """
    pre_candles: 급등 직전 5개 (오래된 → 최신).
    길이 5가 아니면 앞을 0으로 패딩하거나 잘라냄.
    """
    N = 5
    if len(pre_candles) > N:
        pre_candles = pre_candles[-N:]
    elif len(pre_candles) < N:
        # 데이터 부족 → 앞을 빈 캔들로 채움
        dummy = Candle(ts="", open=1.0, high=1.0, low=1.0, close=1.0, volume=0)
        pre_candles = [dummy] * (N - len(pre_candles)) + list(pre_candles)

    atr = _atr5(pre_candles)
    avg_vol = float(np.mean([c.volume for c in pre_candles])) or 1.0

    vec = []
    for c in pre_candles:
        # 방향
        if c.close > c.open * 1.001:
            direction = 1.0
        elif c.close < c.open * 0.999:
            direction = -1.0
        else:
            direction = 0.0

        body       = abs(c.close - c.open) / atr
        upper_tail = (c.high - max(c.open, c.close)) / atr
        lower_tail = (min(c.open, c.close) - c.low) / atr
        vol_ratio  = c.volume / avg_vol

        vec.extend([direction, body, upper_tail, lower_tail, vol_ratio])

    return np.array(vec, dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
