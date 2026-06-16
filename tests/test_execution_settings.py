from __future__ import annotations

import pytest

from src.config.schema import ExecutionMode, MakerUnfilledAction
from src.execution.settings import (
    decode_execution,
    encode_execution,
    execution_defaults_from_settings,
    execution_runtime_to_config,
    validate_execution_payload,
)


def test_execution_defaults_from_settings(settings):
    defaults = execution_defaults_from_settings(settings)

    assert defaults.entry_mode == ExecutionMode.MARKET_TAKER.value
    assert defaults.max_order_retries == settings.execution.max_order_retries
    assert defaults.rate_limit_backoff == settings.execution.rate_limit_backoff


def test_execution_payload_accepts_maker_controls(settings):
    defaults = execution_defaults_from_settings(settings)

    updated = validate_execution_payload({
        "entry_mode": "MAKER_FIRST",
        "maker_timeout_seconds": 12,
        "maker_max_requotes": 3,
        "maker_price_offset_bps": 2.5,
        "maker_unfilled_action": "FALLBACK_MARKET",
        "market_slippage_bps_per_symbol": {"btcusdt": 9},
    }, defaults, allowed_symbols={"BTCUSDT"})

    assert updated.entry_mode == ExecutionMode.MAKER_FIRST.value
    assert updated.maker_unfilled_action == MakerUnfilledAction.FALLBACK_MARKET.value
    assert updated.market_slippage_bps_per_symbol == {"BTCUSDT": 9.0}


def test_execution_payload_rejects_unknown_field(settings):
    defaults = execution_defaults_from_settings(settings)

    with pytest.raises(ValueError, match="unknown runtime execution fields"):
        validate_execution_payload({"recv_window": 10000}, defaults)


def test_execution_payload_rejects_unknown_symbol(settings):
    defaults = execution_defaults_from_settings(settings)

    with pytest.raises(ValueError, match="unknown market_slippage_bps_per_symbol symbols"):
        validate_execution_payload(
            {"market_slippage_bps_per_symbol": {"DOGEUSDT": 12}},
            defaults,
            allowed_symbols={"BTCUSDT"},
        )


def test_execution_encode_decode_roundtrip(settings):
    defaults = execution_defaults_from_settings(settings)
    updated = validate_execution_payload({
        "entry_mode": "MAKER_ONLY",
        "market_slippage_bps_per_symbol": {"BTCUSDT": 7},
    }, defaults, allowed_symbols={"BTCUSDT"})

    decoded = decode_execution(
        encode_execution(updated),
        defaults,
        allowed_symbols={"BTCUSDT"},
    )

    assert decoded == updated


def test_execution_runtime_to_config_preserves_fixed_fields(settings):
    defaults = execution_defaults_from_settings(settings)
    runtime = validate_execution_payload({
        "entry_mode": "MAKER_FIRST",
        "maker_unfilled_action": "FALLBACK_MARKET",
    }, defaults, allowed_symbols={"BTCUSDT"})

    config = execution_runtime_to_config(runtime, settings.execution)

    assert config.entry_mode is ExecutionMode.MAKER_FIRST
    assert config.emergency_exit_mode is ExecutionMode.MARKET_TAKER
    assert config.attach_sl_tp is True
