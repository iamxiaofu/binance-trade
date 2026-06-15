"""市价单滑点护栏：下单前用盘口估算冲击价，超阈值拒单。"""
from __future__ import annotations

import pytest

from src.config.schema import ExecutionConfig
from src.execution.executor import Executor


class _FakeFilters:
    def __init__(self):
        self.tick_size = 0.01


class _FakeClient:
    def __init__(self, bids, asks):
        self._bids = bids
        self._asks = asks
        self.filters = _FakeFilters()

    async def fetch_order_book(self, symbol, limit=20):
        return {"bids": self._bids, "asks": self._asks}

    async def setup_symbol(self, symbol, leverage):
        return None

    async def create_order(self, *a, **k):  # pragma: no cover
        raise RuntimeError("should not be called in preflight test")


def _exec_cfg(*, default: float, per: dict[str, float] | None = None):
    return ExecutionConfig(
        entry_mode="MARKET_TAKER", normal_exit_mode="MARKET_TAKER",
        emergency_exit_mode="MARKET_TAKER", maker_time_in_force="GTX",
        maker_timeout_seconds=15, maker_poll_seconds=1,
        maker_max_requotes=4, maker_price_offset_bps=5,
        rate_limit_backoff=1.5, max_order_retries=3, recv_window=5000,
        market_slippage_bps=default,
        market_slippage_bps_per_symbol=per or {},
    )


async def test_slippage_within_limit_allows():
    """盘口深度足、impact < limit → 允许。"""
    cfg = _exec_cfg(default=8.0)
    ex = Executor.__new__(Executor)
    ex._cfg = cfg
    ex._client = _FakeClient(
        bids=[[66.60, 5], [66.59, 10], [66.58, 20]],
        asks=[[66.62, 5], [66.63, 10], [66.64, 20]],
    )
    # 卖 0.5 张，ref=66.61，吃 66.60 价 0.5 张 → impact=66.60 → slippage=(66.61-66.60)/66.61*1e4=1.5bps < 8
    ok, est, reason = await ex._preflight_market_slippage(
        symbol="SOLUSDT", side="sell", ref_price=66.61, qty=0.5,
    )
    assert ok, reason
    assert abs(est - 66.60) < 1e-9


async def test_slippage_exceeds_limit_rejects():
    """需要吃 3 档、滑点 > 8 bps → 拒单。"""
    cfg = _exec_cfg(default=8.0)
    ex = Executor.__new__(Executor)
    ex._cfg = cfg
    ex._client = _FakeClient(
        bids=[[66.60, 1], [66.50, 1], [66.40, 1], [66.30, 1]],
        asks=[[66.62, 1], [66.63, 1], [66.64, 1], [66.65, 1]],
    )
    # 卖 3 张：1@66.60 + 1@66.50 + 1@66.40 → impact=(66.60+66.50+66.40)/3=66.50
    # ref=66.61 → slippage=(66.61-66.50)/66.61*1e4=16.5bps > 8 → 拒
    ok, est, reason = await ex._preflight_market_slippage(
        symbol="SOLUSDT", side="sell", ref_price=66.61, qty=3.0,
    )
    assert not ok
    assert est == pytest.approx(66.50, abs=1e-6)
    assert "16.5bps" in reason or "16" in reason
    assert "limit 8" in reason


async def test_slippage_per_symbol_override():
    """SOL 用 per_symbol=10，9.5 bps 应当允许。"""
    cfg = _exec_cfg(default=8.0, per={"SOLUSDT": 10.0})
    ex = Executor.__new__(Executor)
    ex._cfg = cfg
    ex._client = _FakeClient(
        bids=[[66.50, 1], [66.50, 1], [66.50, 1]],  # impact=66.50
        asks=[[66.62, 1]],
    )
    # ref=66.56, sell 1@66.50 → slippage=(66.56-66.50)/66.56*1e4=9.0bps
    # default 8 bps 会拒，per_symbol=10 bps 允许
    ok, _, reason = await ex._preflight_market_slippage(
        symbol="SOLUSDT", side="sell", ref_price=66.56, qty=1.0,
    )
    assert ok, reason


async def test_slippage_buy_side_uses_asks():
    """买单看 asks，吃卖盘越深 impact 越高。"""
    cfg = _exec_cfg(default=8.0)
    ex = Executor.__new__(Executor)
    ex._cfg = cfg
    ex._client = _FakeClient(
        bids=[[66.60, 1]],
        asks=[[66.62, 1], [66.72, 1], [66.82, 1]],
    )
    # 买 2 张：1@66.62 + 1@66.72 → impact=66.67, ref=66.60
    # slippage=(66.67-66.60)/66.60*1e4=10.5bps > 8 → 拒
    ok, est, reason = await ex._preflight_market_slippage(
        symbol="BTCUSDT", side="buy", ref_price=66.60, qty=2.0,
    )
    assert not ok
    assert est == pytest.approx(66.67, abs=1e-6)


async def test_slippage_book_fetch_failure_rejects():
    """盘口拉取失败 → 开仓 fail-closed。"""
    class _BrokenClient(_FakeClient):
        async def fetch_order_book(self, symbol, limit=20):
            raise RuntimeError("network down")
    cfg = _exec_cfg(default=8.0)
    ex = Executor.__new__(Executor)
    ex._cfg = cfg
    ex._client = _BrokenClient(bids=[], asks=[])
    ok, _est, reason = await ex._preflight_market_slippage(
        symbol="SOLUSDT", side="sell", ref_price=66.61, qty=1.0,
    )
    assert not ok
    assert "orderbook unavailable" in reason


async def test_slippage_depth_insufficient_rejects():
    """盘口深度不够覆盖 qty → 拒绝开仓。"""
    cfg = _exec_cfg(default=8.0)
    ex = Executor.__new__(Executor)
    ex._cfg = cfg
    ex._client = _FakeClient(
        bids=[[66.60, 0.5]],  # 只有 0.5 张深度
        asks=[],
    )
    ok, _est, _reason = await ex._preflight_market_slippage(
        symbol="SOLUSDT", side="sell", ref_price=66.61, qty=5.0,
    )
    assert not ok
