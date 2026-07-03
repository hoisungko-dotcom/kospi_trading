"""
6,333건 급등 직전 패턴 → K-Means 군집화 → 군집별 특성 출력

실행: python -m analysis.clustering
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from sklearn.cluster import KMeans
from sklearn.preprocessing import normalize

STORE_PATH = Path(__file__).parents[1] / "data" / "patterns" / "surge_patterns.jsonl"
OUT_PATH   = Path(__file__).parents[1] / "data" / "patterns" / "clusters_k16.json"

N_CLUSTERS = 16  # 세밀 패턴 분류
RANDOM_STATE = 42


def load_vectors() -> tuple[np.ndarray, list[dict]]:
    records, vecs = [], []
    with STORE_PATH.open() as f:
        for line in f:
            d = json.loads(line)
            records.append(d)
            vecs.append(d["vector"])
    return np.array(vecs, dtype=np.float32), records


def describe_cluster(vecs: np.ndarray) -> dict:
    """군집 벡터들의 평균 특성 해석 (5캔들 × 5차원)."""
    mean = vecs.mean(axis=0)
    candles = []
    for i in range(5):
        base = i * 5
        direction  = mean[base]
        body       = mean[base + 1]
        upper_tail = mean[base + 2]
        lower_tail = mean[base + 3]
        vol_ratio  = mean[base + 4]

        if direction > 0.3:
            shape = "양봉"
        elif direction < -0.3:
            shape = "음봉"
        else:
            shape = "도지"

        candles.append({
            "캔들": f"C{i+1}",
            "형태": shape,
            "몸통/ATR": round(float(body), 2),
            "윗꼬리/ATR": round(float(upper_tail), 2),
            "아랫꼬리/ATR": round(float(lower_tail), 2),
            "거래량배율": round(float(vol_ratio), 2),
        })
    return {"캔들패턴": candles, "평균벡터": mean.tolist()}


def main():
    print("벡터 로드 중...")
    X, records = load_vectors()
    print(f"총 {len(X)}건 로드 완료")

    # 코사인 유사도 기반 클러스터링을 위해 L2 정규화
    X_norm = normalize(X, norm="l2")

    print(f"K-Means 군집화 (k={N_CLUSTERS})...")
    km = KMeans(n_clusters=N_CLUSTERS, random_state=RANDOM_STATE, n_init=10)
    labels = km.fit_predict(X_norm)

    # 군집별 분석
    results = []
    for c in range(N_CLUSTERS):
        idx = np.where(labels == c)[0]
        cluster_vecs = X[idx]
        cluster_recs = [records[i] for i in idx]

        surge_pcts = [r["surge_pct"] for r in cluster_recs]
        vol_ratios = [r["vol_ratio"] for r in cluster_recs]

        desc = describe_cluster(cluster_vecs)
        results.append({
            "군집": c,
            "건수": int(len(idx)),
            "평균급등률": round(float(np.mean(surge_pcts)) * 100, 2),
            "평균거래량배율": round(float(np.mean(vol_ratios)), 1),
            **desc,
        })

    # 평균 급등률 높은 순 정렬
    results.sort(key=lambda x: x["평균급등률"], reverse=True)

    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    print("\n=== 군집별 요약 (급등률 높은 순) ===")
    for r in results:
        candles_summary = " → ".join(
            f"{c['형태']}(V:{c['거래량배율']:.1f}x)" for c in r["캔들패턴"]
        )
        print(
            f"군집{r['군집']:2d}  {r['건수']:4d}건  "
            f"평균급등 {r['평균급등률']:+.2f}%  "
            f"거래량 {r['평균거래량배율']:.1f}x  |  {candles_summary}"
        )

    print(f"\n결과 저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
