"""指标与特征构建测试。"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from src.features.indicators import compute_snapshot, compute_timeframe_brief, rsi


def _klines(n: int, start: float = 100.0, slope: float = 0.5):
    ts = int(time.time() * 1000)
    out = []
    for i in range(n):
        c = start + i * slope + np.sin(i / 5)
        out.append([ts + i * 300_000, c - 0.3, c + 0.5, c - 0.5, c, 10.0])
    return out


def _constant_klines(n: int, price: float = 100.0):
    ts = int(time.time() * 1000)
    return [[ts + i * 300_000, price, price, price, price, 10.0] for i in range(n)]


def test_rsi_pure_uptrend_is_100():
    s = pd.Series([100 + i for i in range(40)], dtype=float)
    assert round(float(rsi(s).iloc[-1]), 1) == 100.0


def test_rsi_pure_downtrend_is_0():
    s = pd.Series([100 - i for i in range(40)], dtype=float)
    assert round(float(rsi(s).iloc[-1]), 1) == 0.0


def test_rsi_oscillating_mid_range():
    s = pd.Series([100 + 5 * np.sin(i / 3) for i in range(60)], dtype=float)
    v = float(rsi(s).iloc[-1])
    assert 20 < v < 80


def test_compute_snapshot_keys_and_finite():
    snap = compute_snapshot(_klines(100))
    expected_core = {
        "ema_fast", "ema_slow", "rsi", "macd",
        "macd_signal", "atr", "boll_upper", "boll_lower",
        "volume", "volume_ma", "volume_ratio",
        "trend_direction", "trend_score",
        "ema_spread_pct", "ema_spread_delta_3", "ema_spread_delta_6",
        "ema_spread_delta_12", "price_vs_ema_fast_pct", "price_vs_ema_slow_pct",
        "return_1_pct", "return_3_pct", "return_6_pct", "return_12_pct",
        "macd_hist", "macd_hist_delta_3", "macd_hist_delta_6",
        "rsi_delta_3", "rsi_delta_6", "atr_pct", "atr_pct_delta_6",
        "boll_mid", "boll_percent_b", "boll_bandwidth_pct",
        "last_range_pct", "last_body_pct", "volume_ratio_delta_3",
        "volume_zscore_20",
    }
    expected_structure = {
        "adx_14", "plus_di_14", "minus_di_14",
        "vwap", "price_vs_vwap_pct", "vwap_slope_pct",
        "swing_high", "swing_low", "dist_to_swing_high_pct",
        "dist_to_swing_low_pct", "range_position_pct", "breakout_state",
        "atr_pct_percentile_96", "boll_bandwidth_percentile_96",
        "upper_wick_pct", "lower_wick_pct", "body_to_range",
        "consecutive_up_count", "consecutive_down_count",
    }
    assert expected_core | expected_structure <= set(snap)
    for k, v in snap.items():
        if k == "trend_direction":
            assert v in {"up", "down", "flat"}
            continue
        if k == "breakout_state":
            assert v in {
                "inside_range", "breakout_up", "breakout_down",
                "failed_breakout_up", "failed_breakout_down",
            }
            continue
        assert np.isfinite(v), f"{k} not finite"


def test_uptrend_ema_fast_above_slow():
    snap = compute_snapshot(_klines(100, slope=0.5))
    assert snap["ema_fast"] > snap["ema_slow"]  # 上升趋势快线在上


def test_bollinger_bands_ordered():
    snap = compute_snapshot(_klines(100))
    assert snap["boll_upper"] > snap["boll_lower"]


def test_volume_metrics_present():
    snap = compute_snapshot(_klines(100))
    assert snap["volume"] == 10.0
    assert snap["volume_ma"] > 0
    assert snap["volume_ratio"] == 1.0  # 恒定量 → 量比=1
    assert snap["volume_zscore_20"] == 0.0


def test_enriched_trend_features_uptrend():
    snap = compute_snapshot(_klines(100, slope=0.5))
    assert snap["trend_direction"] == "up"
    assert snap["trend_score"] > 0
    assert snap["ema_spread_pct"] > 0
    assert snap["price_vs_ema_slow_pct"] > 0
    assert snap["return_6_pct"] > 0
    assert snap["atr_pct"] > 0


def test_enriched_trend_features_downtrend():
    snap = compute_snapshot(_klines(100, start=200.0, slope=-0.5))
    assert snap["trend_direction"] == "down"
    assert snap["trend_score"] < 0
    assert snap["ema_spread_pct"] < 0
    assert snap["price_vs_ema_slow_pct"] < 0
    assert snap["return_6_pct"] < 0


def test_enriched_features_constant_market_are_neutral_and_finite():
    snap = compute_snapshot(_constant_klines(100))
    assert snap["trend_direction"] == "flat"
    assert snap["trend_score"] == 0.0
    assert snap["boll_percent_b"] == 0.5
    assert snap["boll_bandwidth_pct"] == 0.0
    assert snap["volume_zscore_20"] == 0.0
    for k, v in snap.items():
        if k in {"trend_direction", "breakout_state"}:
            continue
        assert np.isfinite(v), f"{k} not finite"


def test_timeframe_brief_uptrend():
    brief = compute_timeframe_brief(_klines(100, slope=0.5), "1h")
    assert brief["timeframe"] == "1h"
    assert brief["trend"] == "up"
    assert brief["ema_fast"] > brief["ema_slow"]
    assert brief["swing_high"] > brief["swing_low"]
    assert brief["breakout_state"] in {
        "inside_range", "breakout_up", "breakout_down",
        "failed_breakout_up", "failed_breakout_down",
    }


def test_timeframe_brief_downtrend():
    brief = compute_timeframe_brief(_klines(100, start=200.0, slope=-0.5), "15m")
    assert brief["trend"] == "down"
