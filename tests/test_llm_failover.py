"""主备 fallback 链测试：源1失败→源2兜底；全失败→HOLD；单源行为不变。"""
from __future__ import annotations

import asyncio

from src.llm.client import LLMTrace
from src.llm.failover import LLMFailoverClient
from src.llm.schema import (
    Action,
    IndicatorSnapshot,
    MarketContext,
    PositionSnapshot,
    TradeDecision,
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


class _FakeClient:
    """假 LLMClient：按 status 返回 ok 决策或 degraded HOLD。"""

    def __init__(self, *, status, action=Action.OPEN_LONG):
        self._status = status
        self._action = action
        self.closed = False

    async def decide_with_trace(self, ctx):
        if self._status == "ok":
            d = TradeDecision(
                symbol=ctx.symbol, action=self._action, confidence=0.8,
                size_pct=0.1, leverage=2, stop_loss_pct=0.02,
                take_profit_pct=0.04, reason="ok",
            )
            return d, LLMTrace(user_prompt="p", request_json="{}",
                               response_json='{"final_decision": {}}', status="ok")
        d = TradeDecision.safe_hold(ctx.symbol, "boom")
        return d, LLMTrace(user_prompt="p", request_json="{}",
                           response_json='{"final_decision": {}}',
                           status="degraded", error="boom")

    async def decide(self, ctx):
        d, _ = await self.decide_with_trace(ctx)
        return d

    async def close(self):
        self.closed = True


def test_primary_ok_no_fallback():
    chain = LLMFailoverClient([
        ("main", _FakeClient(status="ok")),
        ("backup", _FakeClient(status="ok")),
    ])
    d, trace = asyncio.run(chain.decide_with_trace(_ctx()))
    assert d.action is Action.OPEN_LONG
    assert trace.status == "ok"
    assert trace.source_name == "main"
    assert trace.fallback_used is False


def test_failover_to_backup():
    chain = LLMFailoverClient([
        ("main", _FakeClient(status="degraded")),
        ("backup", _FakeClient(status="ok")),
    ])
    d, trace = asyncio.run(chain.decide_with_trace(_ctx()))
    assert d.action is Action.OPEN_LONG
    assert trace.status == "ok"
    assert trace.source_name == "backup"
    assert trace.fallback_used is True
    # failover 信息进 response_json
    assert "failover" in trace.response_json


def test_all_sources_fail_degrades_hold():
    chain = LLMFailoverClient([
        ("main", _FakeClient(status="degraded")),
        ("backup", _FakeClient(status="degraded")),
    ])
    d, trace = asyncio.run(chain.decide_with_trace(_ctx()))
    assert d.action is Action.HOLD
    assert trace.status == "degraded"
    assert "all 2 sources failed" in trace.error


def test_single_source_chain():
    chain = LLMFailoverClient([("main", _FakeClient(status="ok"))])
    d, trace = asyncio.run(chain.decide_with_trace(_ctx()))
    assert d.action is Action.OPEN_LONG
    assert trace.fallback_used is False
    assert trace.source_name == "main"


def test_close_closes_all():
    c1, c2 = _FakeClient(status="ok"), _FakeClient(status="ok")
    chain = LLMFailoverClient([("main", c1), ("backup", c2)])
    asyncio.run(chain.close())
    assert c1.closed and c2.closed


def test_empty_chain_holds():
    chain = LLMFailoverClient([])
    d, trace = asyncio.run(chain.decide_with_trace(_ctx()))
    assert d.action is Action.HOLD
    assert trace.status == "degraded"
