from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

import pandas as pd

from kospi_bot_v2.domain.models import MarketSnapshot
from kospi_bot_v2.market.indicators import add_indicators


class MarketDataProvider(ABC):
    @abstractmethod
    def load_universe_frame(self) -> pd.DataFrame:
        """Return OHLCV rows for the current evaluation universe."""

    @abstractmethod
    def market_snapshot(self, frame: pd.DataFrame) -> MarketSnapshot:
        """Return broad market context for the same timestamp."""


class CsvMarketDataProvider(MarketDataProvider):
    """CSV-backed provider for shadow runs and repeatable comparisons."""

    def __init__(self, path: Path):
        self.path = path

    def load_universe_frame(self) -> pd.DataFrame:
        df = pd.read_csv(self.path)
        required = {"symbol", "name", "timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"CSV missing columns: {', '.join(sorted(missing))}")
        return add_indicators(df)

    def market_snapshot(self, frame: pd.DataFrame) -> MarketSnapshot:
        latest = frame.sort_values("timestamp").groupby("symbol").tail(1)
        if "close_prev" in latest.columns:
            joined = latest.copy()
        else:
            previous = frame.sort_values("timestamp").groupby("symbol").nth(-2).reset_index()
            joined = latest.merge(previous[["symbol", "close"]], on="symbol", how="left", suffixes=("", "_prev"))
        joined["change_pct"] = ((joined["close"] / joined["close_prev"]) - 1.0).fillna(0) * 100

        kospi = joined[joined["symbol"].str.startswith("KSPI")]
        kosdaq = joined[joined["symbol"].str.startswith("KDQ")]
        investable = joined[~joined["symbol"].str.startswith(("KSPI", "KDQ"))]

        def mean_change(part: pd.DataFrame) -> float:
            if part.empty:
                return float(joined["change_pct"].mean())
            return float(part["change_pct"].mean())

        kospi_row = kospi.iloc[-1] if not kospi.empty else joined.iloc[-1]
        kosdaq_row = kosdaq.iloc[-1] if not kosdaq.empty else joined.iloc[-1]
        advance_ratio = float((investable["change_pct"] > 0).mean()) if not investable.empty else 0.5
        volatility_pct = float(investable["atr_pct"].fillna(0.02).mean() * 100) if not investable.empty else 2.0

        return MarketSnapshot(
            timestamp=pd.to_datetime(joined["timestamp"].max()).to_pydatetime(),
            kospi_change_pct=mean_change(kospi),
            kosdaq_change_pct=mean_change(kosdaq),
            advance_ratio=advance_ratio,
            volatility_pct=volatility_pct,
            kospi_above_sma20=bool(kospi_row["close"] >= kospi_row.get("sma20", kospi_row["close"])),
            kosdaq_above_sma20=bool(kosdaq_row["close"] >= kosdaq_row.get("sma20", kosdaq_row["close"])),
        )


def write_sample_csv(path: Path) -> None:
    """Create a tiny deterministic sample universe for smoke tests."""

    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    symbols = [
        ("KSPI", "KOSPI", 2600, 1.001),
        ("KDQ", "KOSDAQ", 850, 0.999),
        ("005930", "삼성전자", 70000, 1.002),
        ("000660", "SK하이닉스", 172000, 1.006),
        ("035720", "카카오", 53000, 0.998),
        ("122630", "KODEX 레버리지", 18000, 1.004),
        ("252670", "KODEX 200선물인버스2X", 2100, 0.996),
    ]
    start = pd.Timestamp("2026-05-01 09:00:00")
    for day in range(24):
        for symbol, name, base, drift in symbols:
            noise = 1 + ((day % 5) - 2) * 0.002
            close = base * (drift ** day) * noise
            if symbol == "252670" and day > 18:
                close *= 1.015 ** (day - 18)
            if symbol == "000660" and day > 15:
                close *= 1.012 ** (day - 15)
            volume = int(1_000_000 + day * 25000 + (base % 1000) * 100)
            if day == 23 and symbol in {"005930", "000660"}:
                volume = int(volume * 2.4)
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "timestamp": (start + pd.Timedelta(days=day)).isoformat(),
                    "open": round(close * 0.995, 2),
                    "high": round(close * 1.018, 2),
                    "low": round(close * 0.988, 2),
                    "close": round(close, 2),
                    "volume": volume,
                }
            )
    pd.DataFrame(rows).to_csv(path, index=False)
