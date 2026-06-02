from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from kospi_bot_v2.domain.models import Signal, Trade


@dataclass(frozen=True)
class LegacySummary:
    log_path: Path
    buy_count: int
    sell_count: int
    stop_count: int
    partial_profit_count: int
    recent_lines: list[str]


BUY_PATTERNS = (
    re.compile(r"매수 주문"),
    re.compile(r"매수 성공"),
    re.compile(r"BUY"),
)
SELL_PATTERNS = (
    re.compile(r"매도 주문"),
    re.compile(r"매도 성공"),
    re.compile(r"SELL"),
)


def summarize_legacy_log(path: Path, tail_lines: int = 400) -> LegacySummary:
    if not path.exists():
        return LegacySummary(path, 0, 0, 0, 0, ["legacy log not found"])

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail_lines:]
    buy_count = sum(1 for line in lines if any(pattern.search(line) for pattern in BUY_PATTERNS))
    sell_count = sum(1 for line in lines if any(pattern.search(line) for pattern in SELL_PATTERNS))
    stop_count = sum(1 for line in lines if "손절" in line or "STOP_LOSS" in line)
    partial_profit_count = sum(1 for line in lines if "부분익절" in line or "PARTIAL_PROFIT" in line)
    interesting = [
        line
        for line in lines
        if any(keyword in line for keyword in ("매수", "매도", "손절", "익절", "성과", "CRITICAL"))
    ][-20:]
    return LegacySummary(path, buy_count, sell_count, stop_count, partial_profit_count, interesting)


def append_comparison_section(report_path: Path, legacy: LegacySummary, signals: list[Signal], trades: list[Trade]) -> None:
    shadow_buys = sum(1 for trade in trades if trade.action.value == "BUY")
    shadow_sells = sum(1 for trade in trades if trade.action.value == "SELL")
    text = [
        "",
        "## Legacy Bot Comparison",
        "",
        f"- Legacy log: `{legacy.log_path}`",
        f"- Legacy buy-like lines: `{legacy.buy_count}`",
        f"- Legacy sell-like lines: `{legacy.sell_count}`",
        f"- Legacy stop-like lines: `{legacy.stop_count}`",
        f"- Legacy partial-profit-like lines: `{legacy.partial_profit_count}`",
        f"- V2 signals: `{len(signals)}`",
        f"- V2 virtual buys: `{shadow_buys}`",
        f"- V2 virtual sells: `{shadow_sells}`",
        "",
        "### Recent Legacy Lines",
        "",
    ]
    if legacy.recent_lines:
        text.extend(f"- {line}" for line in legacy.recent_lines)
    else:
        text.append("- No recent legacy events")
    with report_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(text) + "\n")
