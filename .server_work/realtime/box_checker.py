"""
BoxChecker v2 — 5분봉 박스 돌파 사전 필터
==========================================
운영 경로에서 사용하는 단일 박스 전략 필터다.

핵심 조건:
  1. 1분봉을 5분봉으로 집계해 박스 구조를 계산
  2. 박스 폭/길이/저점 상승/터치 군집 조건 충족
  3. C 등급 박스는 거부
  4. 현재 5분봉이 박스 상단을 실질적으로 돌파해야 함
  5. 일봉 배경 필터가 켜져 있으면 하락 배경을 추가로 거름

과거 v1 로직은 운영 경로에서 제외됐고, legacy 문서로만 관리한다.
"""
from __future__ import annotations

import os
import logging
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# ── 일봉 API (박스 배경 필터용) ───────────────────────────────────────────
_DAILY_CACHE: dict[str, tuple[list, float]] = {}   # stk_cd → (candles, fetch_time)
_DAILY_CACHE_TTL = 4 * 3600   # 4시간 캐시


def _fetch_daily_candles(stk_cd: str) -> list:
    """KIS 실전 일봉 우선, 실패 시 Kiwoom 폴백 캐시 조회."""
    now_ts = time.time()
    if stk_cd in _DAILY_CACHE:
        candles, fetched_at = _DAILY_CACHE[stk_cd]
        if now_ts - fetched_at < _DAILY_CACHE_TTL:
            return candles

    try:
        from collector.kis_real_client import kis_enabled, get_daily_candles
        if kis_enabled():
            candles = get_daily_candles(stk_cd, days=60)
            _DAILY_CACHE[stk_cd] = (candles, now_ts)
            return candles
    except Exception as e:
        logger.debug("KIS 일봉 조회 실패 %s: %s", stk_cd, e)

    try:
        from collector.kiwoom_client import _get_token, BASE
        import requests as _req
        today = datetime.now(KST).strftime("%Y%m%d")
        hdrs = {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": f"Bearer {_get_token()}",
            "api-id": "ka10081",
        }
        r = _req.post(
            f"{BASE}/api/dostk/chart",
            json={"stk_cd": stk_cd, "base_dt": today, "qry_tp": "0", "upd_stkpc_tp": "1"},
            headers=hdrs, timeout=10,
        )
        d = r.json()
        if d.get("return_code") != 0:
            _DAILY_CACHE[stk_cd] = ([], now_ts)
            return []

        raw = d.get("stk_dt_pole_chart_qry", [])
        candles = []
        for row in reversed(raw):
            if not isinstance(row, dict):
                continue
            def p(s): return float(str(s).lstrip("+-").replace(",", "")) if s else 0.0
            c = {
                "dt": row.get("dt", ""),
                "open": p(row.get("open_pric", 0)),
                "high": p(row.get("high_pric", 0)),
                "low": p(row.get("low_pric", 0)),
                "close": p(row.get("cur_prc", 0)),
            }
            if c["close"] > 0:
                candles.append(c)

        _DAILY_CACHE[stk_cd] = (candles, now_ts)
        return candles

    except Exception as e:
        logger.debug("일봉 조회 실패 %s: %s", stk_cd, e)
        _DAILY_CACHE[stk_cd] = ([], now_ts)
        return []


# ── BoxChecker ────────────────────────────────────────────────────────────

class BoxChecker:
    """5분봉 Box Breakout v2 사전 필터."""

    def __init__(self) -> None:
        self.entry_start_hhmm = os.getenv("BOX_ENTRY_START_HHMM", "0905").strip() or "0905"
        entry_end_hhmm = os.getenv("BOX_ENTRY_END_HHMM", "").strip()
        if not entry_end_hhmm:
            legacy_end_hour = os.getenv("BOX_ENTRY_END_HOUR", "15").strip() or "15"
            entry_end_hhmm = f"{int(legacy_end_hour):02d}00"
        self.entry_end_hhmm = entry_end_hhmm
        self.min_close_above_box_pct = float(os.getenv("BOX_MIN_CLOSE_ABOVE_PCT", "0.15"))
        self.min_breakout_body_ratio = float(os.getenv("BOX_MIN_BREAKOUT_BODY_RATIO", "0.35"))
        self.breakout_volume_ratio = float(os.getenv("BOX_BREAKOUT_VOLUME_RATIO", "2.5"))
        self.aggregate_minutes = int(os.getenv("BOX_AGG_MINUTES", "5") or 5)
        self.cluster_tolerance_pct = float(os.getenv("BOX_CLUSTER_TOLERANCE_PCT", "0.3"))
        self.min_height = float(os.getenv("BOX_MIN_HEIGHT_PCT", "0.6"))
        self.max_height = float(os.getenv("BOX_MAX_HEIGHT_PCT", "2.0"))
        self.pref_min_h = float(os.getenv("BOX_PREFERRED_MIN_HEIGHT_PCT", "0.8"))
        self.pref_max_h = float(os.getenv("BOX_PREFERRED_MAX_HEIGHT_PCT", "1.5"))
        self.min_length = int(os.getenv("BOX_MIN_LENGTH", "8") or 8)
        self.max_length = int(os.getenv("BOX_MAX_LENGTH", "36") or 36)
        # 저점 상승
        self.require_rising = os.getenv("BOX_REQUIRE_RISING_LOWS", "true").lower() == "true"
        self.rising_thresh  = float(os.getenv("BOX_RISING_LOW_RATIO", "0.5"))
        # 돌파 버퍼
        self.breakout_buf  = float(os.getenv("BOX_BREAKOUT_BUF", "0.002"))
        # 일봉 필터
        self.daily_enabled     = os.getenv("BOX_DAILY_ENABLED",          "true").lower() == "true"
        self.daily_min_height  = float(os.getenv("BOX_DAILY_MIN_HEIGHT_PCT", "3.0"))
        self.daily_max_height  = float(os.getenv("BOX_DAILY_MAX_HEIGHT_PCT", "8.0"))

        logger.info(
            "BoxChecker v2 로드 (5분봉 기준 | 진입 %s 이전 | 박스폭 %.1f~%.1f%% | 길이 %d~%d봉 | 저점상승필수=%s | 일봉필터=%s)",
            self.entry_end_hhmm,
            self.min_height, self.max_height, self.min_length, self.max_length,
            self.require_rising, self.daily_enabled,
        )

    # ── 공개 API ──────────────────────────────────────────────────────────

    def check(self, candles: list, stk_cd: str = "") -> tuple[bool, dict]:
        """
        메인 진입점.

        Args:
            candles: 시간순(과거→최신) Candle 리스트. 마지막 봉이 현재(돌파 후보)봉.
            stk_cd:  일봉 배경 필터를 위한 종목코드 (빈 문자열이면 일봉 필터 스킵).

        Returns:
            (통과 여부, 상세 정보 dict)
        """
        info = {
            "is_valid_box":    False,
            "box_height_pct":  0.0,
            "box_length":      0,
            "box_high":        0.0,
            "box_low":         0.0,
            "box_grade":       "C",
            "avg_box_volume":  0.0,
            "is_rising_lows":  False,
            "breakout_ready":  False,
            "breakout_close_pct": 0.0,
            "breakout_body_ratio": 0.0,
            "breakout_volume_strength": 0.0,
            "reject_reason":   "",
            "daily_pass":      None,
            "preferred_box":   False,
        }

        # ① 시간 제한
        if self._entry_time_blocked(candles[-1]):
            info["reject_reason"] = "entry_time_blocked"
            return False, info

        five_min = self._aggregate_candles(candles)
        info["timeframe"] = f"{self.aggregate_minutes}min"
        if len(five_min) < self.min_length + 1:
            info["reject_reason"] = "insufficient_data"
            return False, info

        current = five_min[-1]
        box = self._find_box(five_min[:-1])
        if box is None:
            info["reject_reason"] = "invalid_box_structure"
            return False, info

        info["box_height_pct"] = box["height_pct"]
        info["box_length"]     = box["length"]
        info["box_high"]       = box["box_high"]
        info["box_low"]        = box["box_low"]
        info["preferred_box"]  = box["preferred"]
        info["box_grade"]      = box["grade"]
        info["avg_box_volume"] = box["avg_volume"]

        if box["grade"] == "C":
            info["reject_reason"] = "low_box_grade"
            return False, info

        # ④ 저점 상승형 체크
        rising_ratio = self._rising_low_ratio(box["candles"])
        is_rising = rising_ratio >= self.rising_thresh
        info["is_rising_lows"] = is_rising

        if self.require_rising and not is_rising:
            info["reject_reason"] = "flat_or_falling_lows"
            return False, info

        # ⑤ 돌파 여부
        breakout = current.close > box["box_high"] * (1 + self.breakout_buf)
        info["breakout_ready"] = breakout

        if not breakout:
            info["reject_reason"] = "no_breakout"
            return False, info

        breakout_close_pct = (current.close / box["box_high"] - 1.0) * 100 if box["box_high"] > 0 else 0.0
        candle_range = max(current.high - current.low, 1e-9)
        body_ratio = abs(current.close - current.open) / candle_range
        avg_box_vol = box["avg_volume"]
        info["breakout_close_pct"] = round(breakout_close_pct, 3)
        info["breakout_body_ratio"] = round(body_ratio, 3)
        info["breakout_volume_strength"] = round(current.volume / avg_box_vol if avg_box_vol > 0 else 0.0, 3)

        if breakout_close_pct < self.min_close_above_box_pct:
            info["reject_reason"] = "weak_breakout_close"
            return False, info
        if body_ratio < self.min_breakout_body_ratio:
            info["reject_reason"] = "weak_breakout_body"
            return False, info
        if info["breakout_volume_strength"] < self.breakout_volume_ratio:
            info["reject_reason"] = "weak_breakout_volume"
            return False, info

        # ⑥ 일봉 배경 필터
        if self.daily_enabled and stk_cd:
            daily_ok, daily_reason = self._check_daily(stk_cd)
            info["daily_pass"] = daily_ok
            if not daily_ok:
                info["reject_reason"] = daily_reason
                return False, info
        else:
            info["daily_pass"] = True

        info["is_valid_box"] = True
        return True, info

    # ── 내부 메서드 ───────────────────────────────────────────────────────

    def _entry_time_blocked(self, candle) -> bool:
        hhmm = datetime.now(KST).strftime("%H%M")
        if hhmm < self.entry_start_hhmm:
            return True
        return hhmm >= self.entry_end_hhmm

    def preview_box(self, candles: list, stk_cd: str = "") -> dict | None:
        if len(candles) < self.min_length * self.aggregate_minutes:
            return None
        five_min = self._aggregate_candles(candles)
        if len(five_min) < self.min_length:
            return None
        box = self._find_box(five_min)
        if box is None or box["grade"] == "C":
            return None
        rising_ratio = self._rising_low_ratio(box["candles"])
        if self.require_rising and rising_ratio < self.rising_thresh:
            return None
        if self.daily_enabled and stk_cd:
            daily_ok, _ = self._check_daily(stk_cd)
            if not daily_ok:
                return None
        return box

    def _find_box(self, window: list) -> dict | None:
        n = len(window)
        candidates: list[dict] = []
        for length in range(self.min_length, min(n, self.max_length) + 1):
            w = window[-length:]
            lows = [float(c.low) for c in w]
            highs = [float(c.high) for c in w]
            if not lows or min(lows) <= 0:
                continue
            raw_high = max(highs)
            raw_low = min(lows)
            height_pct = (raw_high - raw_low) / raw_low * 100
            if height_pct < self.min_height or height_pct > self.max_height:
                continue
            high_cluster = self._cluster_level(highs, "high")
            low_cluster = self._cluster_level(lows, "low")
            if high_cluster is None or low_cluster is None:
                continue
            box_high, high_touches = high_cluster
            box_low, low_touches = low_cluster
            if high_touches < 3 or low_touches < 2 or box_low <= 0 or box_high <= box_low:
                continue
            height_pct = (box_high - box_low) / box_low * 100
            if height_pct > self.max_height:
                continue
            avg_volume = sum(float(c.volume) for c in w) / max(len(w), 1)
            candidate = {
                "length": length,
                "box_high": round(box_high, 3),
                "box_low": round(box_low, 3),
                "height_pct": round(height_pct, 3),
                "candles": w,
                "preferred": self.pref_min_h <= height_pct <= self.pref_max_h,
                "avg_volume": round(avg_volume, 3),
                "volume_contracting": self._volume_contracting(w),
                "grade": self._grade_box(w, height_pct),
                "high_touches": high_touches,
                "low_touches": low_touches,
            }
            candidates.append(candidate)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                {"A": 2, "B": 1, "C": 0}.get(item["grade"], 0),
                1 if item["preferred"] else 0,
                item["length"],
                -abs(item["height_pct"] - 1.2),
            ),
            reverse=True,
        )
        return candidates[0]

    def _aggregate_candles(self, candles: list) -> list:
        grouped: list = []
        step = self.aggregate_minutes
        for idx in range(0, len(candles), step):
            chunk = candles[idx:idx + step]
            if len(chunk) < step:
                continue
            first = chunk[0]
            last = chunk[-1]
            grouped.append(
                SimpleNamespace(
                    ts=getattr(last, "ts", getattr(first, "ts", "")),
                    open=float(first.open),
                    high=max(float(c.high) for c in chunk),
                    low=min(float(c.low) for c in chunk),
                    close=float(last.close),
                    volume=sum(float(c.volume) for c in chunk),
                )
            )
        return grouped

    def _cluster_level(self, values: list[float], mode: str) -> tuple[float, int] | None:
        if not values:
            return None
        ranked = sorted(values, reverse=(mode == "high"))
        tolerance = self.cluster_tolerance_pct / 100.0
        best_level = None
        best_count = 0
        for pivot in ranked:
            count = sum(1 for value in ranked if abs(value - pivot) / max(pivot, 1e-9) <= tolerance)
            if count > best_count:
                best_level = pivot
                best_count = count
        if best_level is None:
            return None
        return float(best_level), int(best_count)

    @staticmethod
    def _grade_box(candles: list, height_pct: float) -> str:
        length = len(candles)
        if not candles:
            return "C"
        vol_contracting = BoxChecker._volume_contracting(candles)
        if length >= 8 and height_pct <= 1.5 and vol_contracting:
            return "A"
        if length >= 8 and height_pct <= 2.0:
            return "B"
        return "C"

    @staticmethod
    def _volume_contracting(candles: list) -> bool:
        volumes = [float(getattr(c, "volume", 0.0) or 0.0) for c in candles]
        mid = max(1, len(volumes) // 2)
        first_half = sum(volumes[:mid]) / max(len(volumes[:mid]), 1)
        second_half = sum(volumes[mid:]) / max(len(volumes[mid:]), 1)
        return second_half <= first_half * 0.9 if first_half > 0 else False

    @staticmethod
    def _rising_low_ratio(candles: list) -> float:
        """저점이 올라가는 비율 (0~1)."""
        lows = [c.low for c in candles]
        if len(lows) < 2:
            return 1.0
        rising = sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1])
        return rising / (len(lows) - 1)

    def _check_daily(self, stk_cd: str) -> tuple[bool, str]:
        """일봉 배경 필터 — 박스 폭 3~8%, 명백한 하락 추세 제외."""
        daily = _fetch_daily_candles(stk_cd)
        if len(daily) < 8:
            return True, ""   # 데이터 부족 → 통과 (보수적)

        # 최근 30봉에서 가장 적합한 유효 박스 탐지
        window = daily[-30:]
        saw_small = False
        for length in range(len(window), 4, -1):
            w = window[-length:]
            low = min(c["low"] for c in w)
            if low <= 0:
                continue
            h_pct = (max(c["high"] for c in w) - low) / low * 100

            if h_pct > self.daily_max_height:
                continue
            if h_pct < self.daily_min_height:
                saw_small = True
                continue

            # 일봉 하락 추세 체크 (최근 10봉 전반·후반 종가 평균 비교)
            closes = [c["close"] for c in w[-10:]]
            if len(closes) >= 6:
                mid = len(closes) // 2
                avg_first  = sum(closes[:mid])   / mid
                avg_second = sum(closes[mid:])   / (len(closes) - mid)
                if avg_second < avg_first * 0.95:   # 5% 이상 하락
                    return False, "daily_downtrend"

            return True, ""

        if saw_small:
            return False, "daily_box_too_small"
        return True, ""   # 적절한 일봉 박스를 못 찾으면 일단 통과
