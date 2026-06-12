"""LLM 客户端解析与降级测试（不发真实网络请求，注入 FakeProvider）。"""
from __future__ import annotations

import asyncio

import pytest

from src.config.schema import LLMConfig
from src.llm.client import LLMClient
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


class _FakeProvider:
    """注入式 provider：可指定返回 payload、抛错或超时。"""

    name = "fake"
    model = "fake-model"

    def __init__(self, *, payload=None, exc=None, delay=0.0):
        self._payload = payload
        self._exc = exc
        self._delay = delay

    def request_payload(self, *, system, user_prompt, max_tokens):
        return {"messages": [{"role": "user", "content": user_prompt}], "model": self.model}

    async def create(self, *, system, user_prompt, max_tokens):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc:
            raise self._exc
        return {"payload": self._payload}

    def parse(self, resp, expected_symbol):
        from pydantic import ValidationError
        from src.llm.schema import TradeDecision
        if self._payload is None:
            return None
        try:
            return TradeDecision.model_validate(self._payload)
        except ValidationError:
            return None

    async def ping(self, *, max_tokens=16):
        return None

    async def close(self):
        return None


def _client_with(provider) -> LLMClient:
    client = LLMClient(_cfg(), api_key="x")
    client._provider = provider
    return client


_GOOD = {
    "symbol": "BTCUSDT", "action": "HOLD", "confidence": 0.5,
    "size_pct": 0, "leverage": 1, "stop_loss_pct": 0,
    "take_profit_pct": 0, "reason": "ok",
}


def test_parse_valid_decision():
    payload = {**_GOOD, "action": "OPEN_LONG", "confidence": 0.8,
               "size_pct": 0.1, "leverage": 2, "stop_loss_pct": 0.02,
               "take_profit_pct": 0.04, "reason": "uptrend"}
    client = _client_with(_FakeProvider(payload=payload))
    d = client._parse({"x": 1}, "BTCUSDT")
    assert d is not None and d.action is Action.OPEN_LONG


def test_parse_invalid_returns_none():
    bad = {"symbol": "BTCUSDT", "action": "OPEN_LONG", "confidence": 5}  # 越界+缺字段
    client = _client_with(_FakeProvider(payload=bad))
    assert client._parse({"x": 1}, "BTCUSDT") is None


def test_parse_symbol_mismatch_overridden():
    payload = {**_GOOD, "symbol": "ETHUSDT"}
    client = _client_with(_FakeProvider(payload=payload))
    d = client._parse({"x": 1}, "BTCUSDT")
    assert d.symbol == "BTCUSDT"  # 被纠正


def test_decide_degrades_to_hold_on_error():
    """provider.create 抛错 → decide 返回 safe_hold(HOLD)。"""
    client = _client_with(_FakeProvider(exc=RuntimeError("network down")))
    d = asyncio.run(client.decide(_ctx()))
    assert d.action is Action.HOLD
    assert "[degraded]" in d.reason


def test_decide_degrades_on_timeout():
    client = _client_with(_FakeProvider(delay=10))
    client._cfg = _cfg(max_retries=0).model_copy(update={"timeout": 0.05})
    d = asyncio.run(client.decide(_ctx()))
    assert d.action is Action.HOLD


def test_decide_with_trace_records_request_and_response():
    payload = {**_GOOD, "confidence": 0.4, "reason": "mixed"}
    client = _client_with(_FakeProvider(payload=payload))
    client._cfg = _cfg(max_retries=0)
    decision, trace = asyncio.run(client.decide_with_trace(_ctx()))
    assert decision.action is Action.HOLD
    assert "标的: BTCUSDT" in trace.user_prompt
    assert '"messages"' in trace.request_json
    assert '"final_decision"' in trace.response_json


def test_decide_with_trace_records_latency_and_status():
    payload = {**_GOOD}
    client = _client_with(_FakeProvider(payload=payload, delay=0.05))
    client._cfg = _cfg(max_retries=0)
    decision, trace = asyncio.run(client.decide_with_trace(_ctx()))
    assert decision.action is Action.HOLD
    assert trace.status == "ok"
    assert trace.attempts == 1
    assert trace.latency_ms >= 50
    assert trace.error == ""


def test_decide_with_trace_marks_degraded_after_retries():
    client = _client_with(_FakeProvider(exc=RuntimeError("net down")))
    client._cfg = _cfg(max_retries=2)
    decision, trace = asyncio.run(client.decide_with_trace(_ctx()))
    assert decision.action is Action.HOLD
    assert trace.status == "degraded"
    assert trace.attempts >= 1
    assert trace.latency_ms >= 0
    assert "net down" in trace.error
