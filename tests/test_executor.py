"""execution/executor.py 测试：下单、精度拒单、平仓、SL/TP 触发价。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.exchange.filters import SymbolFilters
from src.config.schema import ExecutionMode
from src.execution.executor import Executor, realized_pnl
from src.llm.schema import Action, TradeDecision


class FakeClient:
    """最小 ExchangeClient 替身。"""

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

    async def cancel_all_condition_orders(self, symbol=None):
        return None

    async def fetch_open_condition_orders(self, symbol):
        return []


def _decision(**kw):
    base = dict(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.9,
                size_pct=0.1, leverage=3, stop_loss_pct=0.02,
                take_profit_pct=0.04, reason="t")
    base.update(kw)
    return TradeDecision(**base)


async def test_open_calls_exchange_and_sets_leverage(settings):
    client = FakeClient()
    ex = Executor(client, settings)
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    assert res["status"] == "filled"
    assert res["filled"] is True
    assert res["opened"] is True
    assert len(client.created) == 1
    assert client.created[0][0:4] == ("BTCUSDT", "buy", 0.05, "market")
    assert client.created[0][4]["newClientOrderId"].startswith("bt-")
    assert res["execution_mode"] == "MARKET_TAKER"
    assert res["liquidity"] == "taker"
    assert client.setup_called == [("BTCUSDT", 3)]


async def test_open_rejects_below_min_notional(settings):
    client = FakeClient()
    ex = Executor(client, settings)
    # qty*price = 0.001*100 = 0.1 < min_notional 5 → 拒单
    res = await ex.open_position(decision=_decision(), qty=0.001, price=100.0)
    assert res["status"] == "rejected"
    assert res["filled"] is False


async def test_sl_tp_trigger_prices_for_long(settings):
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
    assert kinds["SL"]["order_type"] == "STOP_MARKET"
    assert kinds["TP"]["order_type"] == "TAKE_PROFIT_MARKET"
    assert [row[3] for row in client.created] == ["stop_market", "take_profit_market"]


async def test_sl_tp_live_records_placed_status(settings):
    settings.execution.attach_sl_tp = True
    client = FakeClient()
    ex = Executor(client, settings)
    out = await ex.place_sl_tp(decision=_decision(), entry_price=100.0, qty=0.05)
    kinds = {o["kind"]: o for o in out}
    assert kinds["SL"]["status"] == "placed"
    assert kinds["TP"]["status"] == "placed"
    assert kinds["SL"]["filled"] is False
    assert client.created[0][3] == "stop_market"
    assert client.created[1][3] == "take_profit_market"
    assert client.created[0][4]["clientAlgoId"].startswith("bt-")


async def test_sl_tp_disabled(settings):
    settings.execution.attach_sl_tp = False
    ex = Executor(FakeClient(), settings)
    assert await ex.place_sl_tp(decision=_decision(), entry_price=100.0, qty=0.05) == []


class TimeoutButPlacedClient(FakeClient):
    async def create_order(self, symbol, side, amount, order_type="market", price=None, params=None):
        self.created.append((symbol, side, amount, order_type, params))
        raise TimeoutError("backend timeout")

    async def fetch_open_condition_orders(self, symbol):
        symbol, side, amount, order_type, params = self.created[-1]
        order_type_upper = "STOP_MARKET" if order_type == "stop_market" else "TAKE_PROFIT_MARKET"
        return [{
            "id": "algo-1",
            "symbol": "BTC/USDT:USDT",
            "type": order_type_upper,
            "side": side,
            "amount": amount,
            "stopPrice": params["stopPrice"],
            "status": "open",
            "reduceOnly": True,
            "info": {"clientAlgoId": params["clientAlgoId"], "algoStatus": "NEW"},
        }]


async def test_sl_tp_recovers_unknown_create_status(settings):
    client = TimeoutButPlacedClient()
    ex = Executor(client, settings)

    out = await ex.place_protection_orders(
        symbol="BTCUSDT",
        pos_side="short",
        qty=0.05,
        specs=[("SL", "STOP_MARKET", 102.0)],
    )

    assert out[0]["status"] == "placed"
    assert out[0]["id"] == "algo-1"


async def test_close_position_calls_exchange(settings):
    client = FakeClient()
    ex = Executor(client, settings)
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.05, "markPrice": 100.0}
    res = await ex.close_position(pos)
    assert res["closed"] is True
    assert res["side"] == "sell"
    assert len(client.created) == 1
    assert client.created[0][0:4] == ("BTCUSDT", "sell", 0.05, "market")
    assert client.created[0][4]["reduceOnly"] is True
    assert client.created[0][4]["newClientOrderId"].startswith("bt-")


async def test_close_no_position(settings):
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
    client = PartialFillClient(filled=0.03)  # 请求 0.05，仅成交 0.03
    ex = Executor(client, settings)
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    assert res["status"] == "partial"
    assert res["partial"] is True
    assert res["filled"] is True            # 部分成交也算「有仓位」
    assert res["qty"] == pytest.approx(0.03)
    assert res["notional"] == pytest.approx(3.0)


async def test_open_full_fill_when_filled_reported(settings):
    client = PartialFillClient(filled=0.05)  # 全部成交
    ex = Executor(client, settings)
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    assert res["status"] == "filled"
    assert res["partial"] is False
    assert res["qty"] == pytest.approx(0.05)


class MakerPartialClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.canceled: list[tuple] = []

    async def fetch_order_book(self, symbol, limit=5):
        return {"bids": [[100.0, 10]], "asks": [[100.2, 10]]}

    async def create_order(self, symbol, side, amount, order_type="market", price=None, params=None):
        self.created.append((symbol, side, amount, order_type, price, params))
        return {
            "id": "maker-1",
            "price": price,
            "filled": 0.0,
            "status": "open",
            "info": {"orderId": "maker-1", "clientOrderId": params["newClientOrderId"]},
        }

    async def fetch_order(self, symbol, order_id, params=None):
        return {
            "id": order_id,
            "price": 100.0,
            "average": 100.0,
            "filled": 0.02,
            "status": "open",
            "info": {"status": "PARTIALLY_FILLED", "executedQty": "0.02"},
        }

    async def cancel_order(self, symbol, order_id, params=None):
        self.canceled.append((symbol, order_id))
        return {"id": order_id, "status": "canceled"}

    async def fetch_order_trades(self, symbol, order_id, limit=100):
        return [{"fee": {"cost": 0.001, "currency": "USDT"}, "takerOrMaker": "maker"}]


async def test_maker_open_partial_fill_cancels_rest(settings):
    settings.execution.entry_mode = ExecutionMode.MAKER_FIRST
    settings.execution.maker_timeout_seconds = 0.05
    settings.execution.maker_poll_seconds = 0.01
    client = MakerPartialClient()
    ex = Executor(client, settings)

    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)

    assert res["status"] == "partial"
    assert res["qty"] == pytest.approx(0.02)
    assert res["remaining_qty"] == pytest.approx(0.03)
    assert res["order_type"] == "limit"
    assert res["execution_mode"] == "MAKER_FIRST"
    assert res["time_in_force"] == "GTX"
    assert res["liquidity"] == "maker"
    assert res["fee"] == pytest.approx(0.001)
    assert client.created[0][3] == "limit"
    assert client.created[0][5]["timeInForce"] == "GTX"
    assert client.canceled == [("BTCUSDT", "maker-1")]


class MakerUnfilledClient(MakerPartialClient):
    async def fetch_order(self, symbol, order_id, params=None):
        return {
            "id": order_id,
            "price": 100.0,
            "filled": 0.0,
            "status": "open",
            "info": {"status": "NEW", "executedQty": "0"},
        }

    async def fetch_order_trades(self, symbol, order_id, limit=100):
        return []


async def test_maker_open_unfilled_cancels_without_open(settings):
    settings.execution.entry_mode = ExecutionMode.MAKER_ONLY
    settings.execution.maker_timeout_seconds = 0.01
    settings.execution.maker_poll_seconds = 0.001
    settings.execution.maker_max_requotes = 0
    client = MakerUnfilledClient()
    ex = Executor(client, settings)

    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)

    assert res["status"] == "canceled"
    assert res["filled"] is False
    assert res["opened"] is False
    assert res["execution_mode"] == "MAKER_ONLY"
    assert client.canceled == [("BTCUSDT", "maker-1")]


async def test_close_partial_fill(settings):
    client = PartialFillClient(filled=0.02, requested_average=110.0)
    ex = Executor(client, settings)
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.05,
           "entryPrice": 100.0, "markPrice": 110.0}
    res = await ex.close_position(pos)
    assert res["status"] == "partial"
    assert res["qty"] == pytest.approx(0.02)
    assert res["entry_price"] == pytest.approx(100.0)
    assert res["pos_side"] == "long"
