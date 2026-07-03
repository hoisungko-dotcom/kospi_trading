"""
Step 2: 급등 후 exit 타이밍 분석

각 군집별 급등 이벤트 샘플 → 급등 이후 5봉 수익률 추적
→ 최고점 도달 봉수, 평균 최대수익, 최적 exit 타이밍 출력

실행: python -m analysis.exit_analyzer --per-cluster 30
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import numpy as np

from collector.kiwoom_client import get_min_chart, parse_candle
from collector.surge_detector import Candle

STORE_PATH   = Path(__file__).parents[1] / "data" / "patterns" / "surge_patterns.jsonl"
CLUSTER_PATH = Path(__file__).parents[1] / "data" / "patterns" / "clusters.json"
OUT_PATH     = Path(__file__).parents[1] / "data" / "patterns" / "exit_analysis.json"

LOOK_FORWARD = 5   # 급등 후 몇 봉까지 추적


def load_records_by_cluster(cluster_path: Path, store_path: Path) -> dict[int, list[dict]]:
    """군집 레이블 → 급등 레코드 리스트."""
    clusters_data = json.loads(cluster_path.read_text())
    n_clusters = len(clusters_data)

    # 클러스터 중심 벡터 로드
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import normalize

    records, vecs = [], []
    with store_path.open() as f:
        for line in f:
            d = json.loads(line)
            records.append(d)
            vecs.append(d["vector"])

    X = np.array(vecs, dtype=np.float32)
    X_norm = normalize(X, norm="l2")
    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    labels = km.fit_predict(X_norm)

    by_cluster: dict[int, list[dict]] = {i: [] for i in range(n_clusters)}
    for rec, label in zip(records, labels):
        by_cluster[int(label)].append(rec)
    return by_cluster


def analyze_exit(stk_cd: str, surge_ts: str) -> dict | None:
    """급등 직후 5봉 수익률 계산."""
    try:
        rows = get_min_chart(stk_cd, tic_scope="1", max_pages=1)
    except Exception:
        return None

    if not rows:
        return None

    candles = [Candle(**parse_candle(r)) for r in reversed(rows)]

    # surge_ts와 일치하는 봉 위치 찾기
    surge_idx = next((i for i, c in enumerate(candles) if c.ts == surge_ts), None)
    if surge_idx is None or surge_idx + LOOK_FORWARD >= len(candles):
        return None

    surge_close = candles[surge_idx].close   # 급등 봉 실제 종가
    if surge_close <= 0:
        return None

    returns = []
    for k in range(1, LOOK_FORWARD + 1):
        future = candles[surge_idx + k]
        ret = (future.close - surge_close) / surge_close
        returns.append(ret)

    if not returns:
        return None

    peak_idx  = int(np.argmax(returns))
    peak_ret  = returns[peak_idx]
    final_ret = returns[-1]

    return {
        "peak_candle": peak_idx + 1,       # 몇 번째 봉에서 최고점
        "peak_return": round(peak_ret * 100, 3),   # 최고 수익률 %
        "final_return": round(final_ret * 100, 3), # 5봉 후 수익률 %
        "returns": [round(r * 100, 3) for r in returns],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-cluster", type=int, default=30, help="군집당 샘플 수")
    ap.add_argument("--delay", type=float, default=0.2)
    args = ap.parse_args()

    print("군집별 레코드 분류 중...")
    by_cluster = load_records_by_cluster(CLUSTER_PATH, STORE_PATH)

    results = {}
    for cluster_id, records in sorted(by_cluster.items()):
        sample = random.sample(records, min(args.per_cluster, len(records)))
        exits  = []

        for rec in sample:
            res = analyze_exit(rec["stk_cd"], rec["ts"])
            time.sleep(args.delay)
            if res:
                exits.append(res)

        if not exits:
            print(f"군집{cluster_id}: 데이터 부족")
            continue

        peak_candles  = [e["peak_candle"] for e in exits]
        peak_returns  = [e["peak_return"] for e in exits]
        final_returns = [e["final_return"] for e in exits]

        summary = {
            "군집": cluster_id,
            "샘플수": len(exits),
            "최고점_평균봉수": round(float(np.mean(peak_candles)), 1),
            "최고점_최대수익_평균": round(float(np.mean(peak_returns)), 3),
            "5봉후_수익_평균": round(float(np.mean(final_returns)), 3),
            "양봉마감_비율": round(sum(1 for r in final_returns if r > 0) / len(final_returns), 2),
        }
        results[cluster_id] = summary
        print(
            f"군집{cluster_id:2d}  샘플{len(exits):3d}건  "
            f"최고점 {summary['최고점_평균봉수']:.1f}봉째  "
            f"평균최대 {summary['최고점_최대수익_평균']:+.3f}%  "
            f"5봉후 {summary['5봉후_수익_평균']:+.3f}%  "
            f"양봉마감 {summary['양봉마감_비율']:.0%}"
        )

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n결과 저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
