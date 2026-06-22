"""LLM 输出 schema 解析与降级测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.llm.schema import Action, TradeDecision

VALID = {
    "symbol": "BTCUSDT",
    "action": "OPEN_LONG",
    "confidence": 0.8,
    "size_pct": 0.1,
    "leverage": 2,
    "stop_loss_pct": 0.02,
    "take_profit_pct": 0.04,
    "reason": "trend up",
}


def test_valid_parses():
    d = TradeDecision.model_validate(VALID)
    assert d.action is Action.OPEN_LONG
    assert d.is_open is True


def test_symbol_uppercased():
    d = TradeDecision.model_validate({**VALID, "symbol": "btcusdt"})
    assert d.symbol == "BTCUSDT"


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({**VALID, "unexpected": 1})


def test_missing_field_rejected():
    bad = dict(VALID)
    del bad["leverage"]
    with pytest.raises(ValidationError):
        TradeDecision.model_validate(bad)


def test_confidence_out_of_range():
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({**VALID, "confidence": 1.5})


def test_leverage_physical_upper_bound():
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({**VALID, "leverage": 200})


def test_leverage_zero_rejected():
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({**VALID, "leverage": 0})


def test_invalid_action():
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({**VALID, "action": "MOON"})


def test_reason_accepts_expanded_limit():
    d = TradeDecision.model_validate({**VALID, "reason": "x" * 1000})
    assert len(d.reason) == 1000


def test_reason_too_long():
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({**VALID, "reason": "x" * 1001})


def test_safe_hold_degrade():
    d = TradeDecision.safe_hold("ETHUSDT", "llm timeout")
    assert d.action is Action.HOLD
    assert d.symbol == "ETHUSDT"
    assert d.confidence == 0.0
    assert d.leverage == 1
    assert d.is_open is False
    assert "[degraded]" in d.reason


def test_safe_hold_long_reason_truncated():
    d = TradeDecision.safe_hold("BTCUSDT", "x" * 1200)
    assert len(d.reason) <= 1000


def test_json_schema_generated():
    schema = TradeDecision.json_schema_for_llm()
    assert schema["additionalProperties"] is False  # extra=forbid
    assert "leverage" in schema["properties"]
    assert schema["properties"]["reason"]["maxLength"] == 1000
    assert "0.012" in schema["properties"]["stop_loss_pct"]["description"]
    assert "小数×100" in schema["properties"]["stop_loss_pct"]["description"]
    assert "2.00%" in schema["properties"]["take_profit_pct"]["description"]
    assert "OPEN_LONG 的 SL 低于 entry_ref" in schema["properties"]["reason"]["description"]
    assert "OPEN_SHORT 的 SL 高于 entry_ref" in schema["properties"]["reason"]["description"]


def test_multi_take_profit_targets_parse_and_replace_legacy_target():
    decision = TradeDecision.model_validate({
        **VALID,
        "take_profit_pct": 0.0,
        "take_profit_targets": [
            {"leg_id": "TP1", "price_distance_pct": 0.02, "position_pct": 0.5},
            {"leg_id": "TP2", "price_distance_pct": 0.04, "position_pct": 0.5},
        ],
    })
    assert decision.schema_version == 2
    assert [target.leg_id for target in decision.effective_take_profit_targets] == ["TP1", "TP2"]


def test_legacy_take_profit_becomes_single_effective_target():
    decision = TradeDecision.model_validate(VALID)
    assert decision.schema_version == 1
    assert decision.effective_take_profit_targets[0].position_pct == 1.0


def test_legacy_and_multi_take_profit_are_mutually_exclusive():
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({
            **VALID,
            "take_profit_targets": [
                {"price_distance_pct": 0.06, "position_pct": 0.5},
            ],
        })


def test_multi_take_profit_total_and_order_are_validated():
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({
            **VALID,
            "take_profit_pct": 0.0,
            "take_profit_targets": [
                {"price_distance_pct": 0.04, "position_pct": 0.6},
                {"price_distance_pct": 0.02, "position_pct": 0.6},
            ],
        })
