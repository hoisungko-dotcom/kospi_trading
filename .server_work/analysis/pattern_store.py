"""
패턴 저장소 — 급등 이벤트를 벡터로 변환해 누적 저장,
유사 패턴 검색(코사인 유사도) 제공.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from analysis.vectorizer import candles_to_vector, cosine_similarity
from collector.surge_detector import SurgeEvent

STORE_PATH = Path(__file__).parents[1] / "data" / "patterns" / "surge_patterns.jsonl"
logger = logging.getLogger(__name__)


@dataclass
class PatternRecord:
    stk_cd:    str
    ts:        str        # 급등 캔들 타임스탬프
    surge_pct: float
    vol_ratio: float
    vector:    list[float]


class PatternStore:
    def __init__(self, path: Path = STORE_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records: list[PatternRecord] = []
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                self._records.append(PatternRecord(**d))
        logger.info("패턴 로드: %d건", len(self._records))

    def add(self, event: SurgeEvent) -> PatternRecord:
        vec = candles_to_vector(event.pre_candles)
        rec = PatternRecord(
            stk_cd=event.stk_cd,
            ts=event.surge_candle.ts,
            surge_pct=round(event.surge_pct, 6),
            vol_ratio=round(event.vol_ratio, 4),
            vector=vec.tolist(),
        )
        self._records.append(rec)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        return rec

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        min_sim: float = 0.85,
    ) -> list[tuple[float, PatternRecord]]:
        results = []
        for rec in self._records:
            sim = cosine_similarity(query_vector, np.array(rec.vector, dtype=np.float32))
            if sim >= min_sim:
                results.append((sim, rec))
        results.sort(key=lambda x: x[0], reverse=True)
        return results[:top_k]

    @property
    def count(self) -> int:
        return len(self._records)
