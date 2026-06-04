"""技术指标计算（pandas/numpy 实现，避免 pandas-ta 的版本脆弱性）。

输入统一为按时间升序的收盘价/最高/最低序列，返回最新一根的指标值。
所有函数对长度不足的输入返回 NaN，由调用方处理降级。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    # 无下跌（avg_loss=0）时 RSI 约定为 100；无上涨时为 0
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = close.rolling(window=period).mean()
    std = close.rolling(window=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, lower


def _last_float(series: pd.Series, default: float = 0.0) -> float:
    if series is None or len(series) == 0:
        return default
    val = series.iloc[-1]
    return float(val) if pd.notna(val) else default


def compute_snapshot(klines: list[list[float]]) -> dict[str, float]:
    """从 OHLCV K 线计算指标快照 dict（供 IndicatorSnapshot 使用）。

    klines: [[ts, open, high, low, close, volume], ...] 时间升序。
    """
    df = pd.DataFrame(klines, columns=["ts", "open", "high", "low", "close", "volume"])
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    ema_fast = ema(close, 12)
    ema_slow = ema(close, 26)
    macd_line, signal_line = macd(close)
    upper, lower = bollinger(close)
    atr_series = atr(high, low, close)
    rsi_series = rsi(close)

    # 成交量：最新量、20 均量、放量比
    vol_last = _last_float(vol)
    vol_ma = _last_float(vol.rolling(window=20).mean(), vol_last)
    vol_ratio = (vol_last / vol_ma) if vol_ma > 0 else 1.0

    return {
        "ema_fast": _last_float(ema_fast),
        "ema_slow": _last_float(ema_slow),
        "rsi": _last_float(rsi_series, 50.0),
        "macd": _last_float(macd_line),
        "macd_signal": _last_float(signal_line),
        "atr": _last_float(atr_series),
        "boll_upper": _last_float(upper, _last_float(close)),
        "boll_lower": _last_float(lower, _last_float(close)),
        "volume": vol_last,
        "volume_ma": vol_ma,
        "volume_ratio": round(vol_ratio, 3),
    }


def compute_timeframe_brief(klines: list[list[float]], timeframe: str) -> dict:
    """计算单一周期的精简指标 + 趋势判断（供多周期共振）。"""
    df = pd.DataFrame(klines, columns=["ts", "open", "high", "low", "close", "volume"])
    close = df["close"]
    ef = _last_float(ema(close, 12))
    es = _last_float(ema(close, 26))
    macd_line, signal_line = macd(close)
    rsi_v = _last_float(rsi(close), 50.0)
    # 趋势：EMA 快慢线关系 + 间距
    if ef > es * 1.001:
        trend = "up"
    elif ef < es * 0.999:
        trend = "down"
    else:
        trend = "flat"
    return {
        "timeframe": timeframe,
        "ema_fast": ef,
        "ema_slow": es,
        "rsi": rsi_v,
        "macd": _last_float(macd_line),
        "macd_signal": _last_float(signal_line),
        "trend": trend,
    }
