"""size_pct 硬上限在 system prompt / user prompt 必须显式出现，防止回归。"""
from __future__ import annotations

from src.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from src.llm.schema import (
    IndicatorSnapshot, MarketContext, PositionSnapshot,
)


def _ctx(pct: float = 0.2) -> MarketContext:
    return MarketContext(
        symbol="BTCUSDT",
        timestamp=1,
        last_price=62000.0,
        mark_price=62001.0,
        funding_rate=0.0001,
        change_24h_pct=1.2,
        recent_klines=[[i, 1, 2, 0.5, 1.5, 100] for i in range(20)],
        micro_kline_interval="1m",
        micro_klines=[[i, 1, 2, 0.5, 1.5, 10] for i in range(30)],
        indicators=IndicatorSnapshot(
            ema_fast=62000, ema_slow=61900, rsi=55, macd=10, macd_signal=8, macd_hist=2,
            atr=120, atr_pct=0.2, boll_mid=62000, boll_upper=62500, boll_lower=61500,
            volume=100, volume_ma=80, volume_ratio=1.25,
            trend_direction="up", trend_score=0.5,
        ),
        position=PositionSnapshot(has_position=False),
        available_margin=5018.0,
        max_leverage_allowed=5,
        account_equity=5018.0,
        max_order_margin_abs=5018.0 * pct,
        max_order_margin_pct=pct,
        max_loss_per_trade_abs=502,
    )


def test_system_prompt_explains_size_pct_hard_cap():
    """system 决策原则 6 必须提到硬上限 + 拒单。"""
    assert "max_order_margin_pct" in SYSTEM_PROMPT, "system 决策原则缺 max_order_margin_pct 引用"
    # "硬上限" + "拒单" 必须同时出现（强表达）
    assert "硬上限" in SYSTEM_PROMPT
    assert "直接拒单" in SYSTEM_PROMPT or "拒单" in SYSTEM_PROMPT


def test_user_prompt_shows_pct_and_abs():
    """user prompt 必须显式给出百分比与绝对值并列。"""
    p = build_user_prompt(_ctx(pct=0.2))
    # 百分比形式（20.0%）
    assert "20.0%" in p
    # 绝对值
    assert "1003.60 USDT" in p
    # "硬上限" 字面
    assert "硬上限" in p
    # "size_pct ≤" 表达式
    assert "size_pct ≤" in p


def test_user_prompt_pct_changes_with_config():
    """配置改了 max_order_margin_pct，user prompt 里的百分比必须跟着变。"""
    p_low = build_user_prompt(_ctx(pct=0.10))
    p_high = build_user_prompt(_ctx(pct=0.30))
    assert "10.0%" in p_low
    assert "30.0%" in p_high
    # 绝对值也跟着变
    assert "501.80 USDT" in p_low
    assert "1505.40 USDT" in p_high
