"""시장 국면 판단 — KOSPI 추세/변동성 + 매크로 필터 (환율/외국인)

국면:
  STRONG  — KOSPI가 SMA20 대비 5%+ 위, 변동성 정상  → score 기준 완화
  NORMAL  — 추세 양호, 변동성 정상                  → 기본 기준
  CAUTION — 추세 하락 or 변동성 과열                → score 기준 강화 (차단 아님)
  BLOCKED — 환율 급등 + 외국인 이탈 + 추세 하락 동시 → 신규 진입 차단
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

import pandas as pd
import FinanceDataReader as fdr

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    STRONG  = "STRONG"
    NORMAL  = "NORMAL"
    CAUTION = "CAUTION"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class MarketSnapshot:
    regime:        Regime
    kospi_close:   float
    kospi_sma20:   float
    trend_gap_pct: float
    vol_pct:       float
    usdkrw:        float | None
    usdkrw_5d_chg: float | None
    frgn_trend:    str | None    # 'bull' | 'bear' | None
    note:          str = ""

    def score_threshold(self, base: float) -> float:
        """regime별 score 기준 반환."""
        if self.regime == Regime.STRONG:
            return max(48, base - 8)
        if self.regime == Regime.NORMAL:
            return base
        if self.regime == Regime.CAUTION:
            return base + 7
        return base + 15   # BLOCKED: 매우 높게 (사실상 진입 희귀)

    def max_positions_bonus(self) -> int:
        """STRONG에서 포지션 한도 보너스."""
        return 2 if self.regime == Regime.STRONG else 0


class RegimeDetector:
    def detect(self) -> MarketSnapshot:
        try:
            end   = datetime.today()
            start = end - timedelta(days=70)
            df = fdr.DataReader("KS11",
                                start.strftime("%Y-%m-%d"),
                                end.strftime("%Y-%m-%d"))
            if df is None or len(df) < 20:
                logger.warning("KOSPI 데이터 부족 — CAUTION 반환")
                return self._fallback()

            close = df["Close"].astype(float)
            high  = df["High"].astype(float)
            low   = df["Low"].astype(float)

            sma20 = close.rolling(20).mean()
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low  - close.shift()).abs(),
            ], axis=1).max(axis=1)
            atr14 = tr.rolling(14).mean()

            last_close = float(close.iloc[-1])
            last_sma20 = float(sma20.iloc[-1])
            last_atr   = float(atr14.iloc[-1])
            vol_pct    = last_atr / last_close * 100
            trend_gap  = (last_close - last_sma20) / last_sma20 * 100 if last_sma20 else 0.0
            trend_ok   = last_close >= last_sma20

            base_vol   = float(os.getenv("MARKET_VOL_THRESHOLD",            "2.5"))
            strong_gap = float(os.getenv("MARKET_VOL_STRONG_TREND_GAP_PCT", "5.0"))

            if trend_ok and vol_pct <= base_vol:
                tech = Regime.STRONG if trend_gap >= strong_gap else Regime.NORMAL
            else:
                tech = Regime.CAUTION

            # ── 매크로 필터 ───────────────────────────────────────────────
            usdkrw = usdkrw_5d_chg = None
            frgn   = None
            macro_block = macro_caution = False
            note = ""

            try:
                usdkrw, usdkrw_5d_chg, macro_block, macro_caution, note = self._check_fx()
            except Exception as e:
                logger.debug("환율 조회 실패: %s", e)
            try:
                frgn = self._check_frgn()
            except Exception as e:
                logger.debug("외국인 수급 조회 실패: %s", e)

            # 환율 주의 + 외국인 이탈 → block 격상
            if not macro_block and macro_caution and frgn == "bear":
                macro_block = True
                note += " + 외국인 이탈"

            # 최종 국면 결합
            if macro_block and tech == Regime.CAUTION:
                final = Regime.BLOCKED        # 기술적 하락 + 매크로 이중 악재
            elif macro_block:
                final = Regime.CAUTION        # 기술적으로 OK → CAUTION으로 완화
            elif macro_caution and tech == Regime.NORMAL:
                final = Regime.CAUTION
            else:
                final = tech

            snap = MarketSnapshot(
                regime=final,
                kospi_close=last_close,
                kospi_sma20=last_sma20,
                trend_gap_pct=trend_gap,
                vol_pct=vol_pct,
                usdkrw=usdkrw,
                usdkrw_5d_chg=usdkrw_5d_chg,
                frgn_trend=frgn,
                note=note,
            )
            logger.info(
                "📊 시장 국면: %s | KOSPI %s / SMA20 %s (%+.1f%%) | 변동성 %.2f%% | "
                "USD/KRW %s | 외국인 %s%s",
                final.value,
                f"{last_close:,.0f}", f"{last_sma20:,.0f}", trend_gap, vol_pct,
                f"{usdkrw:,.0f}" if usdkrw else "N/A",
                frgn or "N/A",
                f" | {note}" if note else "",
            )
            return snap

        except Exception as e:
            logger.error("시장 국면 판단 오류: %s", e)
            return self._fallback()

    def _check_fx(self) -> tuple:
        caution = float(os.getenv("MACRO_USDKRW_CAUTION",    "1400"))
        block   = float(os.getenv("MACRO_USDKRW_BLOCK",      "1550"))
        rise    = float(os.getenv("MACRO_USDKRW_RISE_BLOCK", "3.0"))

        end   = datetime.today()
        start = end - timedelta(days=25)
        df    = fdr.DataReader("USD/KRW",
                               start.strftime("%Y-%m-%d"),
                               end.strftime("%Y-%m-%d"))
        close = df["Close"].dropna()
        if len(close) < 2:
            return None, None, False, False, ""
        cur    = float(close.iloc[-1])
        prev   = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
        chg5d  = (cur - prev) / prev * 100
        is_blk = cur >= block or chg5d >= rise
        is_cau = not is_blk and cur >= caution
        reason = (
            f"환율 차단 {cur:,.0f}원 (5일 {chg5d:+.1f}%)" if is_blk else
            (f"환율 주의 {cur:,.0f}원" if is_cau else "")
        )
        return cur, chg5d, is_blk, is_cau, reason

    def _check_frgn(self) -> str | None:
        end   = datetime.today()
        start = end - timedelta(days=45)
        df    = fdr.DataReader("FRGN",
                               start.strftime("%Y-%m-%d"),
                               end.strftime("%Y-%m-%d"))
        close = df["Close"].dropna()
        if len(close) < 20:
            return None
        return (
            "bull"
            if float(close.rolling(5).mean().iloc[-1]) >= float(close.rolling(20).mean().iloc[-1])
            else "bear"
        )

    def _fallback(self) -> MarketSnapshot:
        return MarketSnapshot(
            regime=Regime.CAUTION,
            kospi_close=0, kospi_sma20=0,
            trend_gap_pct=0, vol_pct=0,
            usdkrw=None, usdkrw_5d_chg=None, frgn_trend=None,
            note="데이터 없음",
        )
