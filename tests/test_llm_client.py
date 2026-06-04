"""LLM 客户端解析与降级测试（不发真实网络请求）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.config.schema import LLMConfig
from src.llm.client import LLMClient, _TOOL_NAME
from src.llm.schema import Action, IndicatorSnapshot, MarketContext, PositionSnapshot


def _cfg(max_retries=1) -> LLMConfig:
    return LLMConfig(
        model="claude-opus-4-8", timeout=5, max_tokens=512,
        max_retries=max_retries, kline_lookback=100, kline_interval="5m",
        indicators=["ema"],
    )


def _ctx(symbol="BTCUSDT") -> MarketContext:
    return MarketContext(
        symbol=symbol, timestamp=1, last_price=65000, mark_price=65000,
        funding_rate=0.0, change_24h_pct=0.0, recent_klines=[[1, 2, 3, 4, 5, 6]] * 25,
        indicators=IndicatorSnapshot(
            ema_fast=1, ema_slow=2, rsi=55, macd=0.1, macd_signal=0.05,
            atr=10, boll_upper=66000, boll_lower=64000,
        ),
        position=PositionSnapshot(), available_margin=200, max_leverage_allowed=3,
    )


def _tool_use_block(payload: dict):
    return SimpleNamespace(type="tool_use", name=_TOOL_NAME, input=payload)


def _resp(*blocks):
    return SimpleNamespace(content=list(blocks))


def test_parse_valid_decision():
    client = LLMClient(_cfg(), api_key="x")
    payload = {
        "symbol": "BTCUSDT", "action": "OPEN_LONG", "confidence": 0.8,
        "size_pct": 0.1, "leverage": 2, "stop_loss_pct": 0.02,
        "take_profit_pct": 0.04, "reason": "uptrend",
    }
    d = client._parse(_resp(_tool_use_block(payload)), "BTCUSDT")
    assert d is not None and d.action is Action.OPEN_LONG


def test_parse_invalid_returns_none():
    client = LLMClient(_cfg(), api_key="x")
    bad = {"symbol": "BTCUSDT", "action": "OPEN_LONG", "confidence": 5}  # 越界+缺字段
    assert client._parse(_resp(_tool_use_block(bad)), "BTCUSDT") is None


def test_parse_symbol_mismatch_overridden():
    client = LLMClient(_cfg(), api_key="x")
    payload = {
        "symbol": "ETHUSDT", "action": "HOLD", "confidence": 0.5,
        "size_pct": 0, "leverage": 1, "stop_loss_pct": 0,
        "take_profit_pct": 0, "reason": "x",
    }
    d = client._parse(_resp(_tool_use_block(payload)), "BTCUSDT")
    assert d.symbol == "BTCUSDT"  # 被纠正


def test_decide_degrades_to_hold_on_error(monkeypatch):
    """messages.create 抛错 → decide 返回 safe_hold(HOLD)。"""
    client = LLMClient(_cfg(max_retries=1), api_key="x")

    async def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(client._client.messages, "create", boom)
    d = asyncio.run(client.decide(_ctx()))
    assert d.action is Action.HOLD
    assert "[degraded]" in d.reason


def test_decide_degrades_on_timeout(monkeypatch):
    client = LLMClient(_cfg(max_retries=0), api_key="x")

    async def slow(*a, **k):
        await asyncio.sleep(10)

    monkeypatch.setattr(client._client.messages, "create", slow)
    # 把超时压到极短
    client._cfg = _cfg(max_retries=0).model_copy(update={"timeout": 0.05})
    d = asyncio.run(client.decide(_ctx()))
    assert d.action is Action.HOLD
