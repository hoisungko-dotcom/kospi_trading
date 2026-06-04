"""
Daily and weekly league reports.

Output: formatted Korean text → Telegram + local file.
Never calls live broker. Never modifies portfolio state.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kospi_bot_v2.shadow.portfolio import ShadowPortfolio

logger = logging.getLogger(__name__)

_MEDALS = ["🥇", "🥈", "🥉", "4위", "5위", "6위", "7위", "8위"]


def _pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v*100:.2f}%"


def _nav_str(portfolio: "ShadowPortfolio", current_prices: dict | None = None) -> str:
    n = portfolio.nav(current_prices)
    return f"₩{n:,.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# Daily report
# ─────────────────────────────────────────────────────────────────────────────

def build_daily_report(
    portfolios: list["ShadowPortfolio"],
    regime: str,
    kospi_pct: float,
    as_of: date,
    current_prices: dict[str, float] | None = None,
    entries_today: dict[str, list] | None = None,
    exits_today: dict[str, list] | None = None,
    errors: list[str] | None = None,
    data_quality: "object | None" = None,
    trading_day: bool = True,
) -> str:
    current_prices = current_prices or {}
    entries_today  = entries_today or {}
    exits_today    = exits_today or {}
    errors         = errors or []
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    dow = weekdays[as_of.weekday()]

    holiday_note = "" if trading_day else " (휴장일)"
    lines = [
        f"📊 KR 전략 리그 — {as_of.strftime('%Y-%m-%d')} ({dow}){holiday_note}",
        f"레짐: {regime} | KOSPI {_pct(kospi_pct)} | 마감 기준",
        "",
        f"{'전략':<14} {'NAV':>12} {'당일손익':>10} {'누적손익':>10} {'보유':>6} {'거래':>6}",
        "─" * 62,
    ]

    today_str = as_of.isoformat()
    ranked = sorted(portfolios, key=lambda p: p.nav(current_prices), reverse=True)
    for portfolio in ranked:
        strat = portfolio.strategy
        nav = portfolio.nav(current_prices)
        cum_pct = (nav / portfolio.initial_capital) - 1

        # P0-2: daily PnL from NAV equity curve, not sum of trade pnl_pct
        daily_pnl = portfolio.daily_pnl_pct(today_str)

        today_closed = exits_today.get(strat.strategy_id, [])
        n_open     = len(portfolio.positions)
        n_entries  = len(entries_today.get(strat.strategy_id, []))
        n_exits    = len(today_closed)
        trade_str  = f"{n_entries}진/{n_exits}청" if (n_entries or n_exits) else "-"

        # P1-3: cash utilization = open position market value / NAV (not initial-based)
        open_val = sum(
            pos.quantity * current_prices.get(sym, pos.entry_price)
            for sym, pos in portfolio.positions.items()
        )
        cash_util = open_val / nav if nav > 0 else 0.0
        cash_str  = f"{cash_util*100:.0f}%활용"

        label = f"{strat.strategy_id}-{strat.version}"
        lines.append(
            f"{label:<14} {_nav_str(portfolio, current_prices):>12} "
            f"{_pct(daily_pnl):>10} {_pct(cum_pct):>10} "
            f"{n_open}/{portfolio.max_positions}  {trade_str:<8} {cash_str}"
        )

    lines.append("─" * 62)

    # Data-quality section
    if data_quality is not None:
        dq = data_quality
        loops   = getattr(dq, "loops", 0)
        missing = getattr(dq, "missing_prices", 0)
        skipped = getattr(dq, "skipped_no_ind", 0)
        eligible = getattr(dq, "eligible_counts", {})
        elig_str = ", ".join(f"{k}:{v}" for k, v in sorted(eligible.items())) or "-"
        lines.append(
            f"📋 루프:{loops} | 가격누락:{missing} | 지표부족건너뜀:{skipped} | 후보건:{elig_str}"
        )

    # Entries / exits
    all_entries = sum(len(v) for v in entries_today.values())
    if all_entries == 0:
        lines.append("오늘 신규 진입: 없음")
    else:
        lines.append(f"오늘 신규 진입: {all_entries}건")
        for sid, trades in entries_today.items():
            for t in trades:
                lines.append(f"  {sid}: {t.name or t.symbol} @ ₩{t.entry_price:,.0f} ({t.regime})")

    all_exits = sum(len(v) for v in exits_today.values())
    if all_exits > 0:
        lines.append(f"오늘 청산: {all_exits}건")
        for sid, trades in exits_today.items():
            for t in trades:
                sign = "✅" if (t.pnl_pct or 0) > 0 else "❌"
                lines.append(
                    f"  {sign} {sid}: {t.name or t.symbol} "
                    f"{_pct(t.pnl_pct or 0)} ({t.exit_reason})"
                )

    if errors:
        lines.append("")
        lines.append("⚠️ 오류/데이터 이상:")
        for e in errors:
            lines.append(f"  • {e}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Weekly league report
# ─────────────────────────────────────────────────────────────────────────────

def build_weekly_report(
    portfolios: list["ShadowPortfolio"],
    week_label: str,
    period_str: str,
    trading_days_elapsed: int,
    next_eval_date: str,
    current_prices: dict[str, float] | None = None,
) -> str:
    current_prices = current_prices or {}

    stats_list = [p.league_stats() for p in portfolios]
    nav_list = [(p, p.nav(current_prices)) for p in portfolios]
    ranked = sorted(nav_list, key=lambda x: x[1], reverse=True)

    lines = [
        f"📊 KR 전략 리그 주간 보고 — {week_label}",
        f"기간: {period_str} | 경과 거래일: {trading_days_elapsed}일",
        "",
        f"{'순위':<4} {'전략':<14} {'NAV증감':>9} {'PF':>6} {'거래':>6} "
        f"{'승률':>6} {'MDD':>8} {'평균손익':>9}",
        "─" * 70,
    ]

    for idx, (portfolio, nav) in enumerate(ranked):
        strat = portfolio.strategy
        stats = next(s for s in stats_list if s.strategy_id == strat.strategy_id)
        medal = _MEDALS[idx] if idx < len(_MEDALS) else f"{idx+1}위"
        nav_pct = (nav / portfolio.initial_capital) - 1
        pf_str = f"{stats.profit_factor:.2f}" if stats.n_trades > 0 else "-"
        wr_str = f"{stats.win_rate*100:.0f}%" if stats.n_trades > 0 else "-"
        mdd_str = _pct(stats.max_drawdown) if stats.n_trades > 0 else "0.00%"
        avg_str = _pct(stats.avg_pnl_pct) if stats.n_trades > 0 else "-"   # P1-1

        label = f"{strat.strategy_id}-{strat.version} {strat.description[:8]}"
        lines.append(
            f"{medal:<4} {label:<14} {_pct(nav_pct):>9} {pf_str:>6} "
            f"{stats.n_trades:>6} {wr_str:>6} {mdd_str:>8} {avg_str:>9}"
        )

    lines.append("─" * 70)
    lines.append("")

    # Plain Korean conclusions
    cash_portfolio = next((p for p in portfolios if p.strategy.strategy_id == "E"), None)
    cash_nav = cash_portfolio.nav() if cash_portfolio else portfolios[0].initial_capital

    beats_cash = [(p, n) for p, n in ranked if n > cash_nav and p.strategy.strategy_id != "E"]

    lines.append("▶ 이번 주 결론")

    if beats_cash:
        names = ", ".join(f"{p.strategy.strategy_id}-{p.strategy.version}" for p, _ in beats_cash)
        lines.append(f"  현금 기준점을 이긴 전략: {names}")
    else:
        lines.append("  현금 기준점을 이긴 전략: 없음 — 현재 어떤 전략도 무거래보다 낫지 않음")

    min_trades = min((p.league_stats().n_trades for p in portfolios), default=0)
    if trading_days_elapsed < 10:
        lines.append(f"  표본 상태: 운영 초기 ({trading_days_elapsed}거래일) — 성과 평가 보류")
    elif trading_days_elapsed < 20:
        lines.append(f"  표본 상태: 데이터 축적 중 ({trading_days_elapsed}거래일/{min_trades}건) — 방향성 관찰만")
    elif min_trades >= 30:
        lines.append(f"  표본 상태: 첫 성과 평가 가능 ({trading_days_elapsed}거래일/{min_trades}건 이상)")
    else:
        lines.append(f"  표본 상태: 부족 ({min_trades}건 미만) — 최소 30건 필요")

    # Promotion check
    promotion_candidates = []
    for portfolio, nav in ranked:
        if portfolio.strategy.strategy_id == "E":
            continue
        s = portfolio.league_stats()
        nav_pct = (nav / portfolio.initial_capital) - 1
        if (s.profit_factor >= 1.10 and nav_pct > 0
                and nav > cash_nav and s.n_trades >= 30
                and trading_days_elapsed >= 20):
            promotion_candidates.append(portfolio.strategy.strategy_id)

    lines.append("")
    if promotion_candidates:
        lines.append(f"🟢 실전 전환 후보: {', '.join(promotion_candidates)} (추가 검토 필요)")
    else:
        lines.append("🔴 실전 전환 가능 전략: 없음")
    lines.append(f"다음 정기 평가일: {next_eval_date}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Save summary JSON + send Telegram
# ─────────────────────────────────────────────────────────────────────────────

def save_and_notify(
    report_text: str,
    out_path: Path,
    telegram: bool = True,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_text, encoding="utf-8")
    logger.info("Report saved → %s", out_path)

    if telegram:
        try:
            from kospi_bot_v2.notifications import send_telegram
            send_telegram(report_text)
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
