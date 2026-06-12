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


def test_prompt_includes_risk_reason_discipline():
    klines = _klines(100)
    ctx = MarketContext(
        symbol="BNBUSDT",
        timestamp=klines[-1][0],
        last_price=602.56,
        mark_price=602.58,
        funding_rate=0.0,
        change_24h_pct=0.0,
        recent_klines=klines,
        micro_kline_interval="1m",
        micro_klines=_micro_klines(30),
        indicators=IndicatorSnapshot(**compute_snapshot(klines)),
        position=PositionSnapshot(),
        available_margin=5012.88,
        max_leverage_allowed=5,
        account_equity=5012.88,
        max_order_margin_abs=1002.58,
        max_order_margin_pct=0.2,
        max_loss_per_trade_abs=100.26,
    )

    prompt = build_user_prompt(ctx, kline_interval="5m")

    assert "风险字段语义与 reason 必填格式" in prompt
    assert "entry_ref = 最新价 602.56" in prompt
    assert "pct_percent = pct_decimal × 100" in prompt
    assert "0.012 必须写为 1.20%" in prompt
    assert "0.02 必须写为 2.00%" in prompt
    assert "OPEN_LONG: SL=entry_ref×(1-stop_loss_pct)" in prompt
    assert "OPEN_SHORT: SL=entry_ref×(1+stop_loss_pct)" in prompt
    assert "实际成交后系统会用交易所实际价格重算保护单" in prompt
    assert "sl_loss≈notional×stop_loss_pct" in prompt
    assert "equity_loss_pct≈sl_loss÷账户权益×100" in prompt
    assert "R≈tp_profit÷sl_loss" in prompt
    assert "风险换算: entry_ref=..." in prompt
