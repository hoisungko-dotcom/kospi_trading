from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

from kospi_bot_v2.domain.models import MarketRegime, Signal, Trade
from kospi_bot_v2.portfolio.account_snapshot import AccountSnapshot


def write_daily_report(
    report_dir: Path,
    timestamp: datetime,
    regime: MarketRegime,
    signals: list[Signal],
    trades: list[Trade],
    equity: float,
    account_snapshot: AccountSnapshot | None = None,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"shadow_report_{timestamp:%Y%m%d_%H%M%S}.md"
    sells = [trade for trade in trades if trade.pnl_pct is not None]
    win_count = sum(1 for trade in sells if trade.pnl_pct and trade.pnl_pct > 0)
    avg_pnl = sum((trade.pnl_pct or 0) for trade in sells) / len(sells) if sells else 0
    strategy_counts = Counter(signal.strategy.value for signal in signals)

    lines = [
        f"# KOSPI Bot v2 Shadow Report - {timestamp:%Y-%m-%d %H:%M:%S}",
        "",
        f"- Market regime: `{regime.value}`",
        f"- Ending equity: `{equity:,.0f}`",
        f"- Signals: `{len(signals)}`",
        f"- Trades: `{len(trades)}`",
        f"- Closed trades: `{len(sells)}`",
        f"- Win rate: `{(win_count / len(sells) * 100) if sells else 0:.1f}%`",
        f"- Average closed PnL: `{avg_pnl * 100:.2f}%`",
        "",
        "## Signal Mix",
        "",
    ]
    if strategy_counts:
        for strategy, count in strategy_counts.most_common():
            lines.append(f"- `{strategy}`: {count}")
    else:
        lines.append("- No signals")

    lines.extend(["", "## Top Signals", ""])
    for signal in signals[:10]:
        lines.append(
            f"- `{signal.symbol}` {signal.name}: {signal.score:.1f} "
            f"`{signal.strategy.value}` - {signal.reason}"
        )

    lines.extend(["", "## Trades", ""])
    for trade in trades:
        pnl = "" if trade.pnl_pct is None else f" pnl={trade.pnl_pct * 100:.2f}%"
        lines.append(
            f"- `{trade.action.value}` `{trade.symbol}` {trade.name} "
            f"qty={trade.quantity} price={trade.price:,.0f}{pnl} - {trade.reason}"
        )

    lines.extend(["", "## Real Account Snapshot", ""])
    if account_snapshot is None:
        lines.append("- Disabled")
    elif account_snapshot.error:
        lines.append(f"- Error: `{account_snapshot.error}`")
    else:
        lines.extend(
            [
                f"- Mode: `{account_snapshot.mode}`",
                f"- Cash: `{account_snapshot.cash:,.0f}`",
                f"- Stock evaluation: `{account_snapshot.stock_eval:,.0f}`",
                f"- Total evaluation: `{account_snapshot.total_eval:,.0f}`",
                f"- Buy total: `{account_snapshot.buy_total:,.0f}`",
                f"- PnL: `{account_snapshot.pnl_amount:+,.0f}` (`{account_snapshot.pnl_pct:+.2f}%`)",
                f"- Holdings: `{len(account_snapshot.holdings)}`",
                "",
            ]
        )
        for holding in account_snapshot.holdings:
            lines.append(
                f"- `{holding.symbol}` {holding.name}: qty={holding.quantity:,} "
                f"avg={holding.avg_price:,.0f} cur={holding.current_price:,.0f} "
                f"eval={holding.eval_amount:,.0f} pnl={holding.pnl_amount:+,.0f} "
                f"({holding.pnl_pct:+.2f}%)"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
