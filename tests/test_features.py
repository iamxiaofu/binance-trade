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
    expected = {
        "ema_fast", "ema_slow", "rsi", "macd",
        "macd_signal", "atr", "boll_upper", "boll_lower",
        "volume", "volume_ma", "volume_ratio",
    }
    assert set(snap) == expected
    for k, v in snap.items():
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


def test_timeframe_brief_uptrend():
    brief = compute_timeframe_brief(_klines(100, slope=0.5), "1h")
    assert brief["timeframe"] == "1h"
    assert brief["trend"] == "up"
    assert brief["ema_fast"] > brief["ema_slow"]


def test_timeframe_brief_downtrend():
    brief = compute_timeframe_brief(_klines(100, start=200.0, slope=-0.5), "15m")
    assert brief["trend"] == "down"
