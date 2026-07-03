"""키움 연결 + 삼성전자 1분봉 5캔들 출력 테스트."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from collector.kiwoom_client import get_min_chart, parse_candle
from collector.surge_detector import Candle, SurgeDetector
from analysis.vectorizer import candles_to_vector

rows = get_min_chart("005930", max_pages=1)
print(f"수신: {len(rows)}건")

candles = [Candle(**parse_candle(r)) for r in reversed(rows)]
print("\n최근 5캔들:")
for c in candles[-5:]:
    print(f"  {c.ts}  O={c.open:.0f} H={c.high:.0f} L={c.low:.0f} C={c.close:.0f} V={c.volume}")

detector = SurgeDetector(surge_pct=0.01, vol_mult=1.5, lookback=5)
hits = 0
for i in range(len(candles)):
    ev = detector.check("005930", candles[:i+1])
    if ev:
        hits += 1
        vec = candles_to_vector(ev.pre_candles)
        print(f"\n급등 감지 @{ev.surge_candle.ts} +{ev.surge_pct:.2%} vol×{ev.vol_ratio:.1f}")
        print(f"  벡터 shape={vec.shape}  norm={float((vec**2).sum()**0.5):.3f}")

print(f"\n급등 이벤트 총 {hits}건 (임계값 완화 테스트)")
