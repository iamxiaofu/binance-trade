"""技术指标计算（pandas/numpy 实现，避免 pandas-ta 的版本脆弱性）。

输入统一为按时间升序的收盘价/最高/最低序列，返回最新一根的指标值。
长度不足或交易所返回异常值时使用中性默认值，避免把 NaN/inf 送入 LLM。
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


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    """Return ADX, +DI, -DI series using Wilder-style exponential smoothing."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat(
        [(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr_smoothed = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_smoothed
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_smoothed
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100.0
    adx_series = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx_series, plus_di, minus_di


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
    return float(val) if pd.notna(val) and np.isfinite(val) else default


def _lag_float(series: pd.Series, periods: int, default: float = 0.0) -> float:
    if series is None or len(series) <= periods:
        return default
    val = series.iloc[-1 - periods]
    return float(val) if pd.notna(val) and np.isfinite(val) else default


def _pct(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0 or not np.isfinite(denominator):
        return default
    out = numerator / denominator * 100.0
    return float(out) if np.isfinite(out) else default


def _pct_change(series: pd.Series, periods: int) -> float:
    now = _last_float(series)
    prev = _lag_float(series, periods)
    return _pct(now - prev, prev)


def _delta(series: pd.Series, periods: int) -> float:
    now = _last_float(series)
    prev = _lag_float(series, periods, now)
    out = now - prev
    return float(out) if np.isfinite(out) else 0.0


def _sign(value: float, threshold: float) -> int:
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def _percentile_rank(series: pd.Series, value: float, lookback: int = 96) -> float:
    window = series.replace([np.inf, -np.inf], np.nan).dropna().tail(lookback)
    if len(window) == 0 or not np.isfinite(value):
        return 0.0
    return float((window <= value).mean() * 100.0)


def _vwap_series(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series) -> pd.Series:
    typical = (high + low + close) / 3.0
    cum_vol = vol.cumsum().replace(0, np.nan)
    return (typical * vol).cumsum() / cum_vol


def _consecutive_direction(close: pd.Series) -> tuple[int, int]:
    if len(close) < 2:
        return 0, 0
    diffs = close.diff().dropna()
    up = 0
    down = 0
    for value in reversed(diffs.tolist()):
        if value > 0 and down == 0:
            up += 1
        elif value < 0 and up == 0:
            down += 1
        else:
            break
    return up, down


def _swing_stats(high: pd.Series, low: pd.Series, close: pd.Series, lookback: int = 48) -> dict[str, float | str]:
    close_last = _last_float(close)
    if close_last <= 0 or len(close) == 0:
        return {
            "swing_high": 0.0,
            "swing_low": 0.0,
            "dist_to_swing_high_pct": 0.0,
            "dist_to_swing_low_pct": 0.0,
            "range_position_pct": 0.5,
            "breakout_state": "inside_range",
        }
    current_high = _last_float(high, close_last)
    current_low = _last_float(low, close_last)
    prev_high_window = high.iloc[max(0, len(high) - lookback - 1):-1]
    prev_low_window = low.iloc[max(0, len(low) - lookback - 1):-1]
    high_window = high.tail(lookback)
    low_window = low.tail(lookback)
    swing_high = _last_float(pd.Series([high_window.max()]), current_high)
    swing_low = _last_float(pd.Series([low_window.min()]), current_low)
    prev_swing_high = _last_float(pd.Series([prev_high_window.max()]), swing_high)
    prev_swing_low = _last_float(pd.Series([prev_low_window.min()]), swing_low)
    range_width = swing_high - swing_low
    range_position = (close_last - swing_low) / range_width if range_width > 0 else 0.5
    if close_last > prev_swing_high:
        breakout_state = "breakout_up"
    elif close_last < prev_swing_low:
        breakout_state = "breakout_down"
    elif current_high > prev_swing_high and close_last <= prev_swing_high:
        breakout_state = "failed_breakout_up"
    elif current_low < prev_swing_low and close_last >= prev_swing_low:
        breakout_state = "failed_breakout_down"
    else:
        breakout_state = "inside_range"
    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "dist_to_swing_high_pct": _pct(swing_high - close_last, close_last),
        "dist_to_swing_low_pct": _pct(close_last - swing_low, close_last),
        "range_position_pct": float(range_position),
        "breakout_state": breakout_state,
    }


def compute_snapshot(klines: list[list[float]]) -> dict[str, float | str]:
    """从 OHLCV K 线计算指标快照 dict（供 IndicatorSnapshot 使用）。

    klines: [[ts, open, high, low, close, volume], ...] 时间升序。
    """
    df = pd.DataFrame(klines, columns=["ts", "open", "high", "low", "close", "volume"])
    open_, close, high, low, vol = (
        df["open"], df["close"], df["high"], df["low"], df["volume"]
    )

    ema_fast = ema(close, 12)
    ema_slow = ema(close, 26)
    macd_line, signal_line = macd(close)
    upper, lower = bollinger(close)
    boll_mid = close.rolling(window=20).mean()
    atr_series = atr(high, low, close)
    rsi_series = rsi(close)
    adx_series, plus_di_series, minus_di_series = adx(high, low, close)
    vwap = _vwap_series(high, low, close, vol)

    # 成交量：最新量、20 均量、放量比
    vol_last = _last_float(vol)
    vol_ma = _last_float(vol.rolling(window=20).mean(), vol_last)
    vol_ratio = (vol_last / vol_ma) if vol_ma > 0 else 1.0
    volume_ratio_series = vol / vol.rolling(window=20).mean()
    volume_ratio_delta_3 = _delta(volume_ratio_series.replace([np.inf, -np.inf], np.nan), 3)
    vol_std = _last_float(vol.rolling(window=20).std(ddof=0))
    volume_zscore_20 = ((vol_last - vol_ma) / vol_std) if vol_std > 0 else 0.0

    close_last = _last_float(close)
    open_last = _last_float(open_, close_last)
    high_last = _last_float(high, close_last)
    low_last = _last_float(low, close_last)
    ef = _last_float(ema_fast)
    es = _last_float(ema_slow)
    macd_v = _last_float(macd_line)
    signal_v = _last_float(signal_line)
    atr_v = _last_float(atr_series)
    upper_v = _last_float(upper, close_last)
    lower_v = _last_float(lower, close_last)
    mid_v = _last_float(boll_mid, close_last)

    ema_spread_pct_series = (ema_fast - ema_slow) / close.replace(0, np.nan) * 100.0
    macd_hist_series = macd_line - signal_line
    atr_pct_series = atr_series / close.replace(0, np.nan) * 100.0
    boll_bandwidth_series = (upper - lower) / boll_mid.replace(0, np.nan) * 100.0

    ema_spread_pct = _last_float(ema_spread_pct_series)
    price_vs_ema_fast_pct = _pct(close_last - ef, ef)
    price_vs_ema_slow_pct = _pct(close_last - es, es)
    macd_hist_v = _last_float(macd_hist_series)
    atr_pct_v = _last_float(atr_pct_series)

    boll_width = upper_v - lower_v
    boll_percent_b = (close_last - lower_v) / boll_width if boll_width > 0 else 0.5
    boll_bandwidth_pct = _pct(boll_width, mid_v)
    last_range_pct = _pct(high_last - low_last, close_last)
    last_body_pct = _pct(close_last - open_last, open_last)
    candle_range = max(high_last - low_last, 0.0)
    upper_wick_pct = _pct(high_last - max(open_last, close_last), close_last)
    lower_wick_pct = _pct(min(open_last, close_last) - low_last, close_last)
    body_to_range = abs(close_last - open_last) / candle_range if candle_range > 0 else 0.0
    consecutive_up, consecutive_down = _consecutive_direction(close)
    vwap_v = _last_float(vwap, close_last)
    swing = _swing_stats(high, low, close, lookback=48)

    ret_6 = _pct_change(close, 6)
    trend_threshold = max(0.02, atr_pct_v * 0.08)
    trend_inputs = [
        _sign(ema_spread_pct, trend_threshold),
        _sign(price_vs_ema_slow_pct, trend_threshold),
        _sign(macd_hist_v, 0.0),
        _sign(ret_6, trend_threshold),
    ]
    trend_score = sum(trend_inputs) / len(trend_inputs)
    if trend_score >= 0.5:
        trend_direction = "up"
    elif trend_score <= -0.5:
        trend_direction = "down"
    else:
        trend_direction = "flat"

    return {
        "ema_fast": ef,
        "ema_slow": es,
        "rsi": _last_float(rsi_series, 50.0),
        "macd": macd_v,
        "macd_signal": signal_v,
        "atr": atr_v,
        "boll_upper": upper_v,
        "boll_lower": lower_v,
        "volume": vol_last,
        "volume_ma": vol_ma,
        "volume_ratio": round(vol_ratio, 3),
        "trend_direction": trend_direction,
        "trend_score": round(trend_score, 3),
        "ema_spread_pct": round(ema_spread_pct, 4),
        "ema_spread_delta_3": round(_delta(ema_spread_pct_series, 3), 4),
        "ema_spread_delta_6": round(_delta(ema_spread_pct_series, 6), 4),
        "ema_spread_delta_12": round(_delta(ema_spread_pct_series, 12), 4),
        "price_vs_ema_fast_pct": round(price_vs_ema_fast_pct, 4),
        "price_vs_ema_slow_pct": round(price_vs_ema_slow_pct, 4),
        "return_1_pct": round(_pct_change(close, 1), 4),
        "return_3_pct": round(_pct_change(close, 3), 4),
        "return_6_pct": round(ret_6, 4),
        "return_12_pct": round(_pct_change(close, 12), 4),
        "macd_hist": round(macd_hist_v, 6),
        "macd_hist_delta_3": round(_delta(macd_hist_series, 3), 6),
        "macd_hist_delta_6": round(_delta(macd_hist_series, 6), 6),
        "rsi_delta_3": round(_delta(rsi_series, 3), 4),
        "rsi_delta_6": round(_delta(rsi_series, 6), 4),
        "atr_pct": round(atr_pct_v, 4),
        "atr_pct_delta_6": round(_delta(atr_pct_series, 6), 4),
        "atr_pct_percentile_96": round(_percentile_rank(atr_pct_series, atr_pct_v), 2),
        "boll_mid": mid_v,
        "boll_percent_b": round(float(boll_percent_b), 4),
        "boll_bandwidth_pct": round(boll_bandwidth_pct, 4),
        "boll_bandwidth_percentile_96": round(
            _percentile_rank(boll_bandwidth_series, boll_bandwidth_pct), 2
        ),
        "last_range_pct": round(last_range_pct, 4),
        "last_body_pct": round(last_body_pct, 4),
        "upper_wick_pct": round(upper_wick_pct, 4),
        "lower_wick_pct": round(lower_wick_pct, 4),
        "body_to_range": round(float(body_to_range), 4),
        "consecutive_up_count": consecutive_up,
        "consecutive_down_count": consecutive_down,
        "volume_ratio_delta_3": round(volume_ratio_delta_3, 4),
        "volume_zscore_20": round(float(volume_zscore_20), 4),
        "adx_14": round(_last_float(adx_series), 4),
        "plus_di_14": round(_last_float(plus_di_series), 4),
        "minus_di_14": round(_last_float(minus_di_series), 4),
        "vwap": round(vwap_v, 4),
        "price_vs_vwap_pct": round(_pct(close_last - vwap_v, vwap_v), 4),
        "vwap_slope_pct": round(_pct_change(vwap, 12), 4),
        **{
            key: round(float(value), 4) if isinstance(value, float) else value
            for key, value in swing.items()
        },
    }


def compute_timeframe_brief(klines: list[list[float]], timeframe: str) -> dict:
    """计算单一周期的精简指标 + 趋势判断（供多周期共振）。"""
    df = pd.DataFrame(klines, columns=["ts", "open", "high", "low", "close", "volume"])
    close = df["close"]
    high = df["high"]
    low = df["low"]
    ef = _last_float(ema(close, 12))
    es = _last_float(ema(close, 26))
    macd_line, signal_line = macd(close)
    rsi_v = _last_float(rsi(close), 50.0)
    atr_series = atr(high, low, close)
    adx_series, _, _ = adx(high, low, close)
    swing = _swing_stats(high, low, close, lookback=48)
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
        "adx": round(_last_float(adx_series), 4),
        "atr_pct": round(_pct(_last_float(atr_series), _last_float(close)), 4),
        **{
            key: round(float(value), 4) if isinstance(value, float) else value
            for key, value in swing.items()
        },
    }
