"""execution/executor.py 测试：dry-run 行为、精度拒单、平仓、SL/TP 触发价。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.exchange.filters import SymbolFilters
from src.execution.executor import Executor, realized_pnl
from src.llm.schema import Action, TradeDecision


class FakeClient:
    """最小 ExchangeClient 替身。dry_run 路径不会真正调用下单。"""

    def __init__(self):
        self.created: list[tuple] = []
        self.setup_called: list[tuple] = []
        self._f = SymbolFilters(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )

    def filters(self, symbol):
        return self._f

    async def setup_symbol(self, symbol, leverage):
        self.setup_called.append((symbol, leverage))

    async def create_order(self, symbol, side, amount, order_type="market", price=None, params=None):
        self.created.append((symbol, side, amount, order_type, params))
        return {"id": "oid-1", "average": 100.0, "status": "closed"}

    async def fetch_positions(self, symbols=None):
        return []

    async def cancel_all_orders(self, symbol=None):
        return None


def _decision(**kw):
    base = dict(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
                size_pct=0.1, leverage=3, stop_loss_pct=0.02,
                take_profit_pct=0.04, reason="t")
    base.update(kw)
    return TradeDecision(**base)


async def test_open_dry_run_does_not_call_exchange(settings):
    settings.execution.dry_run = True
    client = FakeClient()
    ex = Executor(client, settings)
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    assert res["status"] == "dry_run"
    assert res["filled"] is True
    assert res["opened"] is True
    assert client.created == []          # 未真实下单
    assert client.setup_called == []     # dry_run 不设置杠杆


async def test_open_rejects_below_min_notional(settings):
    settings.execution.dry_run = True
    client = FakeClient()
    ex = Executor(client, settings)
    # qty*price = 0.001*100 = 0.1 < min_notional 5 → 拒单
    res = await ex.open_position(decision=_decision(), qty=0.001, price=100.0)
    assert res["status"] == "rejected"
    assert res["filled"] is False


async def test_open_live_calls_exchange_and_sets_leverage(settings):
    settings.execution.dry_run = False
    client = FakeClient()
    ex = Executor(client, settings)
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    assert res["status"] == "filled"
    assert res["id"] == "oid-1"
    assert len(client.created) == 1
    assert client.setup_called == [("BTCUSDT", 3)]


async def test_sl_tp_trigger_prices_for_long(settings):
    settings.execution.dry_run = True
    settings.execution.attach_sl_tp = True
    client = FakeClient()
    ex = Executor(client, settings)
    out = await ex.place_sl_tp(decision=_decision(), entry_price=100.0, qty=0.05)
    kinds = {o["kind"]: o for o in out}
    assert set(kinds) == {"SL", "TP"}
    # long: SL 在下方(98)，TP 在上方(104)，并按 tick=0.1 规整
    assert kinds["SL"]["price"] == pytest.approx(98.0, abs=0.1)
    assert kinds["TP"]["price"] == pytest.approx(104.0, abs=0.1)
    assert kinds["SL"]["side"] == "sell"


async def test_sl_tp_disabled(settings):
    settings.execution.attach_sl_tp = False
    ex = Executor(FakeClient(), settings)
    assert await ex.place_sl_tp(decision=_decision(), entry_price=100.0, qty=0.05) == []


async def test_close_position_dry_run(settings):
    settings.execution.dry_run = True
    ex = Executor(FakeClient(), settings)
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.05, "markPrice": 100.0}
    res = await ex.close_position(pos)
    assert res["closed"] is True
    assert res["side"] == "sell"


async def test_close_no_position(settings):
    settings.execution.dry_run = True
    ex = Executor(FakeClient(), settings)
    res = await ex.close_position({"symbol": "BTC/USDT:USDT", "contracts": 0})
    assert res["status"] == "rejected"


# ---------- realized_pnl 纯函数 ----------
def test_realized_pnl_long_profit():
    assert realized_pnl(side="long", entry_price=100, exit_price=110, qty=2) == pytest.approx(20.0)


def test_realized_pnl_long_loss():
    assert realized_pnl(side="long", entry_price=100, exit_price=90, qty=3) == pytest.approx(-30.0)


def test_realized_pnl_short_profit():
    # 空头：价格下跌盈利
    assert realized_pnl(side="short", entry_price=100, exit_price=90, qty=2) == pytest.approx(20.0)


def test_realized_pnl_short_loss():
    assert realized_pnl(side="short", entry_price=100, exit_price=110, qty=2) == pytest.approx(-20.0)


def test_realized_pnl_guards_invalid():
    assert realized_pnl(side="long", entry_price=0, exit_price=110, qty=2) == 0.0
    assert realized_pnl(side="long", entry_price=100, exit_price=110, qty=0) == 0.0


# ---------- 部分成交解析 ----------
class PartialFillClient(FakeClient):
    """create_order 返回部分成交。"""
    def __init__(self, filled, requested_average=100.0):
        super().__init__()
        self._filled = filled
        self._avg = requested_average

    async def create_order(self, symbol, side, amount, order_type="market", price=None, params=None):
        self.created.append((symbol, side, amount, order_type, params))
        return {"id": "oid-p", "average": self._avg, "filled": self._filled, "status": "open"}


async def test_open_partial_fill_records_filled_qty(settings):
    settings.execution.dry_run = False
    client = PartialFillClient(filled=0.03)  # 请求 0.05，仅成交 0.03
    ex = Executor(client, settings)
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    assert res["status"] == "partial"
    assert res["partial"] is True
    assert res["filled"] is True            # 部分成交也算「有仓位」
    assert res["qty"] == pytest.approx(0.03)
    assert res["notional"] == pytest.approx(3.0)


async def test_open_full_fill_when_filled_reported(settings):
    settings.execution.dry_run = False
    client = PartialFillClient(filled=0.05)  # 全部成交
    ex = Executor(client, settings)
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    assert res["status"] == "filled"
    assert res["partial"] is False
    assert res["qty"] == pytest.approx(0.05)


async def test_close_partial_fill(settings):
    settings.execution.dry_run = False
    client = PartialFillClient(filled=0.02, requested_average=110.0)
    ex = Executor(client, settings)
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.05,
           "entryPrice": 100.0, "markPrice": 110.0}
    res = await ex.close_position(pos)
    assert res["status"] == "partial"
    assert res["qty"] == pytest.approx(0.02)
    assert res["entry_price"] == pytest.approx(100.0)
    assert res["pos_side"] == "long"
