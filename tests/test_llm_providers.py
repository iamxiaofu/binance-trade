"""Provider 抽象 + schema 内联 测试（不发真实网络请求）。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.llm.providers import build_provider
from src.llm.providers._schema import (
    _TOOL_NAME,
    build_anthropic_tool,
    build_openai_function,
    inline_defs,
)
from src.llm.providers.anthropic_provider import AnthropicProvider
from src.llm.providers.openai_provider import OpenAICompatProvider
from src.llm.schema import Action


_GOOD = {
    "symbol": "BTCUSDT", "action": "OPEN_LONG", "confidence": 0.8,
    "size_pct": 0.1, "leverage": 2, "stop_loss_pct": 0.02,
    "take_profit_pct": 0.04, "reason": "uptrend",
}


def test_build_provider_unknown_raises():
    with pytest.raises(ValueError, match="unknown llm provider"):
        build_provider("grok", model="m", base_url=None, api_key="x", timeout=5)


def test_build_provider_dispatch():
    a = build_provider("anthropic", model="m", base_url=None, api_key="x", timeout=5)
    assert isinstance(a, AnthropicProvider)
    o = build_provider("openai_compatible", model="m", base_url="https://gw", api_key="x", timeout=5)
    assert isinstance(o, OpenAICompatProvider)


def test_inline_defs_removes_refs_and_inlines_enum():
    schema = {
        "$defs": {"Action": {"enum": ["HOLD", "OPEN_LONG"], "type": "string", "title": "Action"}},
        "title": "TradeDecision",
        "type": "object",
        "properties": {
            "action": {"$ref": "#/$defs/Action", "description": "动作"},
            "confidence": {"type": "number"},
        },
    }
    out = inline_defs(schema)
    blob = json.dumps(out)
    assert "$defs" not in blob and "$ref" not in blob
    act = out["properties"]["action"]
    assert act["enum"] == ["HOLD", "OPEN_LONG"]
    assert act["description"] == "动作"  # 引用点兄弟键保留


def test_openai_function_has_no_defs():
    fn = build_openai_function()
    assert fn["function"]["name"] == _TOOL_NAME
    blob = json.dumps(fn["function"]["parameters"])
    assert "$defs" not in blob and "$ref" not in blob


def test_anthropic_tool_keeps_schema():
    tool = build_anthropic_tool()
    assert tool["name"] == _TOOL_NAME
    assert tool["input_schema"]["type"] == "object"


def test_anthropic_parse_tool_use():
    p = AnthropicProvider(model="m", base_url=None, api_key="x", timeout=5)
    resp = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=_TOOL_NAME, input=_GOOD)])
    d = p.parse(resp, "BTCUSDT")
    assert d is not None and d.action is Action.OPEN_LONG


def test_anthropic_parse_no_tool_use_returns_none():
    p = AnthropicProvider(model="m", base_url=None, api_key="x", timeout=5)
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")])
    assert p.parse(resp, "BTCUSDT") is None


def _openai_resp(arguments: str):
    fn = SimpleNamespace(arguments=arguments)
    call = SimpleNamespace(function=fn)
    msg = SimpleNamespace(tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_openai_parse_tool_calls():
    p = OpenAICompatProvider(model="m", base_url="https://gw", api_key="x", timeout=5)
    d = p.parse(_openai_resp(json.dumps(_GOOD)), "BTCUSDT")
    assert d is not None and d.action is Action.OPEN_LONG


def test_openai_parse_no_tool_calls_returns_none():
    p = OpenAICompatProvider(model="m", base_url="https://gw", api_key="x", timeout=5)
    msg = SimpleNamespace(tool_calls=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    assert p.parse(resp, "BTCUSDT") is None


def test_openai_parse_bad_json_returns_none():
    p = OpenAICompatProvider(model="m", base_url="https://gw", api_key="x", timeout=5)
    assert p.parse(_openai_resp("{not json"), "BTCUSDT") is None
