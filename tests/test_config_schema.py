"""config/schema.py 测试：运行模式与 DB 路径解析。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config.schema import (
    AccountConfig,
    CycleConfig,
    ExecutionConfig,
    ExecutionMode,
    LLMConfig,
    LoggingConfig,
    NotifyConfig,
    RiskConfig,
    Settings,
    StorageConfig,
    ThrottleConfig,
    Credentials,
)


def _settings(*, mode: str, storage: StorageConfig) -> Settings:
    return Settings(
        mode=mode,
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
            max_order_margin_pct=0.2,
            max_symbol_margin_pct=0.4,
            max_total_margin_pct=0.8,
            max_loss_per_order_margin_pct=30,
            max_drawdown_pct=20,
            daily_max_loss_pct=10,
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
            rate_limit_backoff=1.5,
            max_order_retries=3,
            recv_window=5000,
        ),
        storage=storage,
        notify=NotifyConfig(),
        logging=LoggingConfig(),
    )


def test_storage_template_resolves_testnet_path():
    s = _settings(
        mode="testnet",
        storage=StorageConfig(db_path_template="./data/trade-{mode}.db"),
    )
    assert s.storage.db_path == "./data/trade-testnet.db"


def test_storage_template_resolves_mainnet_path():
    s = _settings(
        mode="mainnet",
        storage=StorageConfig(db_path_template="./data/trade-{mode}.db"),
    )
    assert s.storage.db_path == "./data/trade-mainnet.db"


def test_storage_legacy_db_path_remains_supported():
    s = _settings(mode="testnet", storage=StorageConfig(db_path="./data/trade.db"))
    assert s.storage.db_path == "./data/trade.db"


def test_storage_rejects_template_without_mode_placeholder():
    with pytest.raises(ValidationError):
        StorageConfig(db_path_template="./data/trade.db")


def test_llm_rejects_invalid_micro_kline_interval():
    with pytest.raises(ValidationError):
        LLMConfig(
            model="claude-opus-4-8",
            timeout=30,
            max_tokens=1024,
            max_retries=2,
            kline_lookback=100,
            kline_interval="5m",
            micro_kline_interval="7m",
            indicators=["ema", "rsi"],
        )


def test_execution_legacy_order_type_limit_maps_to_maker_first():
    cfg = ExecutionConfig(
        order_type="LIMIT",
        rate_limit_backoff=1.5,
        max_order_retries=3,
        recv_window=5000,
    )
    assert cfg.entry_mode is ExecutionMode.MAKER_FIRST


def test_execution_defaults_legacy_market_taker():
    cfg = ExecutionConfig(
        rate_limit_backoff=1.5,
        max_order_retries=3,
        recv_window=5000,
    )
    assert cfg.entry_mode is ExecutionMode.MARKET_TAKER


def test_execution_rejects_non_market_emergency_mode():
    with pytest.raises(ValidationError):
        ExecutionConfig(
            entry_mode="MAKER_FIRST",
            emergency_exit_mode="MAKER_ONLY",
            rate_limit_backoff=1.5,
            max_order_retries=3,
            recv_window=5000,
        )


def test_credentials_do_not_require_fixed_llm_key():
    creds = Credentials(binance_api_key="key", binance_api_secret="secret")
    assert creds.anthropic_api_key == ""
