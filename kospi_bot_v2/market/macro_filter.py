from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import FinanceDataReader as fdr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MacroSignal:
    usdkrw:        float | None = None
    usdkrw_5d_chg: float | None = None
    frgn_trend:    str | None   = None   # 'bull' | 'bear' | None
    block:         bool = False
    caution:       bool = False
    reason:        str  = ""


class MacroFilter:
    """환율(USD/KRW) + 외국인 수급(FRGN) 매크로 필터.

    ENV:
        MACRO_USDKRW_CAUTION    : 환율 주의 기준 (기본 1400)
        MACRO_USDKRW_BLOCK      : 환율 차단 기준 (기본 1550)
        MACRO_USDKRW_RISE_BLOCK : 5일 상승률 차단 % (기본 3.0)
    """

    def __init__(self) -> None:
        self._caution_level = float(os.getenv("MACRO_USDKRW_CAUTION",    "1400"))
        self._block_level   = float(os.getenv("MACRO_USDKRW_BLOCK",      "1550"))
        self._rise_block    = float(os.getenv("MACRO_USDKRW_RISE_BLOCK", "3.0"))

    def evaluate(self) -> MacroSignal:
        usdkrw = usdkrw_5d_chg = None
        frgn_trend = None
        block = caution = False
        reasons: list[str] = []

        try:
            usdkrw, usdkrw_5d_chg, fx_block, fx_caution, fx_reason = self._check_usdkrw()
            if fx_block:
                block = True
                reasons.append(fx_reason)
            elif fx_caution:
                caution = True
                reasons.append(fx_reason)
        except Exception as e:
            logger.warning(f"환율 데이터 오류: {e}")

        try:
            frgn_trend = self._check_frgn()
        except Exception as e:
            logger.warning(f"외국인 수급 데이터 오류: {e}")

        # 환율 주의 + 외국인 이탈 → 차단으로 격상
        if not block and caution and frgn_trend == "bear":
            block = True
            caution = False
            reasons.append("외국인 이탈 추세")

        reason = " | ".join(reasons)
        logger.info(
            "매크로 필터: USD/KRW %s (5일 %s) | 외국인 %s | %s",
            f"{usdkrw:,.0f}" if usdkrw else "N/A",
            f"{usdkrw_5d_chg:+.1f}%" if usdkrw_5d_chg is not None else "N/A",
            frgn_trend or "N/A",
            "BLOCK" if block else "CAUTION" if caution else "OK",
        )
        return MacroSignal(
            usdkrw=usdkrw,
            usdkrw_5d_chg=usdkrw_5d_chg,
            frgn_trend=frgn_trend,
            block=block,
            caution=caution,
            reason=reason,
        )

    # ------------------------------------------------------------------
    def _check_usdkrw(self) -> tuple:
        end   = datetime.today()
        start = end - timedelta(days=25)
        df    = fdr.DataReader("USD/KRW", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        close = df["Close"].dropna()
        if len(close) < 2:
            return None, None, False, False, ""

        current = float(close.iloc[-1])
        prev    = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
        chg_5d  = (current - prev) / prev * 100

        is_block   = current >= self._block_level or chg_5d >= self._rise_block
        is_caution = not is_block and current >= self._caution_level

        reason = ""
        if is_block:
            reason = f"환율 차단 {current:,.0f}원 (5일 {chg_5d:+.1f}%)"
        elif is_caution:
            reason = f"환율 주의 {current:,.0f}원"

        return current, chg_5d, is_block, is_caution, reason

    def _check_frgn(self) -> str | None:
        end   = datetime.today()
        start = end - timedelta(days=45)
        df    = fdr.DataReader("FRGN", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        close = df["Close"].dropna()
        if len(close) < 20:
            return None
        ma5  = float(close.rolling(5).mean().iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        return "bull" if ma5 >= ma20 else "bear"
