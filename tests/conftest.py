"""共享 fixtures。"""
from __future__ import annotations

import pytest

from src.config.schema import (
    AccountConfig,
    CycleConfig,
    ExecutionConfig,
    LLMConfig,
    LoggingConfig,
    NotifyConfig,
    RiskConfig,
    Settings,
    StorageConfig,
    ThrottleConfig,
)


@pytest.fixture
def settings() -> Settings:
    """一份合法的最小 Settings，供风控等测试使用。"""
    return Settings(
        mode="testnet",
        symbols=["BTCUSDT"],
        account=AccountConfig(initial_capital=200),
        cycle=CycleConfig(interval="5m", heartbeat_on_skip=True),
        throttle=ThrottleConfig(
            price_change_pct=0.3,
            pnl_alert_pct=1.0,
            trigger_on_order_event=True,
            max_skip_cycles=6,
        ),
        risk=RiskConfig(
            max_leverage=3,
            max_order_margin_pct=0.2,          # 200*0.2=40
            max_symbol_margin_pct=0.4,         # 200*0.4=80
            max_total_margin_pct=0.8,          # 200*0.8=160
            max_loss_per_trade_pct=2,          # 200*2%=4
            max_drawdown_pct=20,
            daily_max_loss_pct=10,             # 200*10%=20
            liq_distance_min_pct=5,
            min_confidence=0.6,
        ),
        llm=LLMConfig(
            model="claude-opus-4-8",
            timeout=30,
            max_tokens=1024,
            max_retries=2,
            kline_lookback=100,
            kline_interval="5m",
            indicators=["ema", "rsi"],
        ),
        execution=ExecutionConfig(
            rate_limit_backoff=1.5, max_order_retries=3, recv_window=5000
        ),
        storage=StorageConfig(db_path="./data/trade.db"),
        notify=NotifyConfig(),
        logging=LoggingConfig(),
    )
