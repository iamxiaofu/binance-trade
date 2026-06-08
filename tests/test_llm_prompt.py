"""LLM prompt 渲染测试。"""
from __future__ import annotations

import numpy as np

from src.features.indicators import compute_snapshot
from src.llm.prompt import build_user_prompt
from src.llm.schema import IndicatorSnapshot, MarketContext, PositionSnapshot


def _klines(n: int):
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        c = 100.0 + i * 0.5 + np.sin(i / 5)
        out.append([ts + i * 300_000, c - 0.3, c + 0.5, c - 0.5, c, 10.0])
    return out


def _micro_klines(n: int):
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        c = 100.0 + i * 0.1
        out.append([ts + i * 60_000, c - 0.1, c + 0.2, c - 0.2, c, 3.0])
    return out


def test_prompt_includes_enriched_main_timeframe_features():
    klines = _klines(100)
    micro = _micro_klines(30)
    ctx = MarketContext(
        symbol="BTCUSDT",
        timestamp=klines[-1][0],
        last_price=klines[-1][4],
        mark_price=klines[-1][4],
        funding_rate=0.0,
        change_24h_pct=0.0,
        recent_klines=klines,
        micro_kline_interval="1m",
        micro_klines=micro,
        indicators=IndicatorSnapshot(**compute_snapshot(klines)),
        position=PositionSnapshot(),
        available_margin=200.0,
        max_leverage_allowed=3,
        account_equity=200.0,
        max_order_margin_abs=40.0,
        max_loss_per_trade_abs=4.0,
    )

    prompt = build_user_prompt(ctx, kline_interval="5m", prompt_kline_count=20,
                               micro_kline_count=30)

    assert "主周期结构化趋势特征" in prompt
    assert "趋势=up" in prompt
    assert "EMA价差=" in prompt
    assert "收益率: 1根=" in prompt
    assert "MACD柱变化" in prompt
    assert "Boll%B=" in prompt
    assert "量能变化" in prompt
    assert "最近20根K线（5m级别）" in prompt
    assert "最近30根微观K线（1m级别）" in prompt
    assert str(round(micro[-1][4], 4)) in prompt
