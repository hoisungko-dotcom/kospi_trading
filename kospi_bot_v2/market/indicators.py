from __future__ import annotations

import pandas as pd


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of OHLCV data with core indicators.

    Required columns: symbol, name, timestamp, open, high, low, close, volume.
    Indicators are calculated per symbol and intentionally kept simple for the
    first shadow version.
    """

    if frame.empty:
        return frame.copy()

    df = frame.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values(["symbol", "timestamp"])

    grouped = df.groupby("symbol", group_keys=False)
    df["sma5"] = grouped["close"].transform(lambda s: s.rolling(5, min_periods=2).mean())
    df["sma20"] = grouped["close"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    df["sma60"] = grouped["close"].transform(lambda s: s.rolling(60, min_periods=20).mean())
    df["high20"] = grouped["high"].transform(lambda s: s.rolling(20, min_periods=5).max())
    df["low20"] = grouped["low"].transform(lambda s: s.rolling(20, min_periods=5).min())
    df["avg_volume20"] = grouped["volume"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    df["close_prev"] = grouped["close"].shift()
    df["return1"] = grouped["close"].pct_change()
    df["return5"] = grouped["close"].pct_change(5)
    df["return20"] = grouped["close"].pct_change(20)

    buy_day = ((df["close"] > df["open"]) & (df["volume"] > df["avg_volume20"] * 1.1)).astype(int)

    def consecutive_count(series: pd.Series) -> pd.Series:
        count = 0
        values: list[int] = []
        for value in series:
            count = count + 1 if value else 0
            values.append(count)
        return pd.Series(values, index=series.index)

    df["consecutive_buy_days"] = buy_day.groupby(df["symbol"]).transform(consecutive_count)

    delta = grouped["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.groupby(df["symbol"]).transform(lambda s: s.rolling(14, min_periods=5).mean())
    avg_loss = loss.groupby(df["symbol"]).transform(lambda s: s.rolling(14, min_periods=5).mean())
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    df["rsi14"] = pd.to_numeric(100 - (100 / (1 + rs)), errors="coerce").fillna(50)

    ema12 = grouped["close"].transform(lambda s: s.ewm(span=12, adjust=False).mean())
    ema26 = grouped["close"].transform(lambda s: s.ewm(span=26, adjust=False).mean())
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df.groupby("symbol")["macd"].transform(lambda s: s.ewm(span=9, adjust=False).mean())

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - grouped["close"].shift()).abs(),
            (df["low"] - grouped["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr14"] = true_range.groupby(df["symbol"]).transform(lambda s: s.rolling(14, min_periods=5).mean())
    df["atr_pct"] = (df["atr14"] / df["close"]).fillna(0.02)

    return df
