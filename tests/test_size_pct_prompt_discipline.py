"""size_pct 硬上限在 system prompt / user prompt 必须显式出现，防止回归。"""
from __future__ import annotations

from src.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from src.llm.schema import (
    IndicatorSnapshot, MarketContext, PositionSnapshot,
)


def _ctx(
    pct: float = 0.2,
    *,
    available_margin: float = 5018.0,
    account_equity: float = 5018.0,
) -> MarketContext:
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
        available_margin=available_margin,
        max_leverage_allowed=5,
        account_equity=account_equity,
        max_order_margin_abs=account_equity * pct,
        max_order_margin_pct=pct,
        max_loss_per_trade_abs=502,
    )


def test_system_prompt_explains_size_pct_hard_cap():
    """system 决策原则 6 必须提到硬上限 + 拒单。"""
    assert "max_order_margin_pct" in SYSTEM_PROMPT, "system 决策原则缺 max_order_margin_pct 引用"
    # "硬上限" + "拒单" 必须同时出现（强表达）
    assert "硬上限" in SYSTEM_PROMPT
    assert "直接拒单" in SYSTEM_PROMPT or "拒单" in SYSTEM_PROMPT
    assert "0.012 必须表述为 1.20%" in SYSTEM_PROMPT
    assert "OPEN_LONG/OPEN_SHORT 的 reason 必须同时写清风险换算" in SYSTEM_PROMPT


def test_user_prompt_shows_pct_and_abs():
    """user prompt 必须显式给出百分比与绝对值并列。"""
    p = build_user_prompt(_ctx(pct=0.2))
    # 百分比形式（20.0%）
    assert "20.0%" in p
    # 绝对值
    assert "1003.60 USDT" in p
    # "硬上限" 字面
    assert "硬上限" in p
    # 硬上限以 margin_used 的绝对金额表达，size_pct 只作为可用保证金比例
    assert "margin_used ≤ 1003.60 USDT" in p
    assert "max_order_margin_pct 0.2000 × 账户权益 5018.00" in p
    assert "size_pct 参考上限" in p
    # 风险换算公式
    assert "pct_percent = pct_decimal × 100" in p
    assert "margin_used=可用保证金×size_pct" in p
    assert "margin_loss_pct≈sl_loss÷margin_used×100" in p
    assert "R≈tp_profit÷sl_loss" in p


def test_user_prompt_pct_changes_with_config():
    """配置改了 max_order_margin_pct，user prompt 里的百分比必须跟着变。"""
    p_low = build_user_prompt(_ctx(pct=0.10))
    p_high = build_user_prompt(_ctx(pct=0.30))
    assert "10.0%" in p_low
    assert "30.0%" in p_high
    # 绝对值也跟着变
    assert "501.80 USDT" in p_low
    assert "1505.40 USDT" in p_high


def test_user_prompt_margin_cap_uses_equity_base():
    """账户权益与可用保证金不一致时，绝对硬上限必须按权益表达。"""
    p = build_user_prompt(_ctx(pct=0.2, available_margin=4000.0, account_equity=5000.0))
    assert "margin_used ≤ 1000.00 USDT" in p
    assert "max_order_margin_pct 0.2000 × 账户权益 5000.00" in p
    assert "0.2000 × 可用保证金 4000.00" not in p
