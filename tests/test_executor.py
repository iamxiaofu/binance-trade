"""execution/executor.py 测试：下单、精度拒单、平仓、SL/TP 触发价。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.exchange.filters import SymbolFilters
from src.config.schema import ExecutionMode
import time

import ccxt.async_support as ccxt
from src.execution.executor import Executor, ProtectionOrderSpec, realized_pnl
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


class WideCloseSlippageClient(FakeClient):
    async def fetch_order_book(self, symbol, limit=20):
        return {
            "bids": [[99.9, 1.0]],
            "asks": [[101.0, 1.0]],
        }


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


async def test_ambiguous_market_open_recovers_by_client_order_id(settings):
    class AmbiguousClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.create_calls = 0
            self.recovered_client_id = ""

        async def create_order(self, symbol, side, amount, order_type="market", price=None, params=None):
            self.create_calls += 1
            self.recovered_client_id = params["newClientOrderId"]
            raise ccxt.NetworkError("response lost")

        async def fetch_order_by_client_id(self, symbol, client_order_id):
            assert client_order_id == self.recovered_client_id
            return {
                "id": "recovered-1", "clientOrderId": client_order_id,
                "filled": 0.05, "average": 100.0, "status": "closed",
            }

    client = AmbiguousClient()
    ex = Executor(client, settings)
    result = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    assert result["status"] == "filled"
    assert result["id"] == "recovered-1"
    assert client.create_calls == 1


def test_fee_summary_deduplicates_duplicate_trade_fee_sources():
    trade = {
        "amount": 2.494,
        "price": 1670.48,
        "fee": {"cost": 1.66647084, "currency": "USDT"},
        "fees": [{"cost": 1.66647084, "currency": "USDT"}],
        "info": {
            "commission": "1.66647084",
            "commissionAsset": "USDT",
            "maker": "false",
        },
    }

    fee, asset, liquidity = Executor._fee_summary({}, [trade])

    assert fee == pytest.approx(1.66647084)
    assert asset == "USDT"
    assert liquidity == "taker"


class MarketOrderWithDelayedTradesClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.fetch_order_trades_calls = 0

    async def create_order(self, symbol, side, amount, order_type="market", price=None, params=None):
        self.created.append((symbol, side, amount, order_type, params))
        return {"id": "9370602752", "status": "closed", "info": {"orderId": "9370602752"}}

    async def fetch_order_trades(self, symbol, order_id, limit=100):
        self.fetch_order_trades_calls += 1
        return [{
            "amount": 2.494,
            "price": 1670.48,
            "fee": {"cost": 1.66647084, "currency": "USDT"},
            "fees": [{"cost": 1.66647084, "currency": "USDT"}],
            "info": {
                "qty": "2.494",
                "price": "1670.48",
                "quoteQty": "4166.17712000",
                "commission": "1.66647084",
                "commissionAsset": "USDT",
                "maker": "false",
            },
        }]


async def test_market_open_uses_mytrades_for_avg_notional_and_fee(settings):
    client = MarketOrderWithDelayedTradesClient()
    ex = Executor(client, settings)

    res = await ex.open_position(
        decision=_decision(symbol="ETHUSDT", action=Action.OPEN_SHORT, leverage=5),
        qty=2.494,
        price=1668.89,
    )

    assert res["status"] == "filled"
    assert res["qty"] == pytest.approx(2.494)
    assert res["price"] == pytest.approx(1670.48)
    assert res["notional"] == pytest.approx(4166.17712)
    assert res["fee"] == pytest.approx(1.66647084)
    assert res["fee_asset"] == "USDT"
    assert res["liquidity"] == "taker"
    assert client.fetch_order_trades_calls == 1


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
    out = await ex.place_sl_tp(decision=_decision(), entry_price=100.0, qty=0.06)
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
    out = await ex.place_sl_tp(decision=_decision(), entry_price=100.0, qty=0.06)
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


async def test_place_multiple_take_profit_legs(settings):
    client = FakeClient()
    ex = Executor(client, settings)

    out = await ex.place_protection_orders(
        symbol="BTCUSDT",
        pos_side="long",
        qty=0.1,
        specs=[
            ProtectionOrderSpec(
                "TP", "TAKE_PROFIT_MARKET", 104.0,
                qty=0.05, leg_id="TP1", position_pct=0.5,
            ),
            ProtectionOrderSpec(
                "TP", "TAKE_PROFIT_MARKET", 108.0,
                qty=0.05, leg_id="TP2", position_pct=0.5,
            ),
        ],
    )

    assert [row["leg_id"] for row in out] == ["TP1", "TP2"]
    assert [row["qty"] for row in out] == pytest.approx([0.05, 0.05])
    assert [row[2] for row in client.created] == pytest.approx([0.05, 0.05])
    assert client.created[0][4]["clientAlgoId"] != client.created[1][4]["clientAlgoId"]


async def test_place_sl_tp_uses_llm_multi_target_plan(settings):
    client = FakeClient()
    ex = Executor(client, settings)
    decision = _decision(
        take_profit_pct=0.0,
        take_profit_targets=[
            {"leg_id": "TP1", "price_distance_pct": 0.04, "position_pct": 0.5},
            {"leg_id": "TP2", "price_distance_pct": 0.08, "position_pct": 0.5},
        ],
    )

    out = await ex.place_sl_tp(decision=decision, entry_price=100.0, qty=0.1)
    take_profits = [row for row in out if row["kind"] == "TP"]

    assert [row["price"] for row in take_profits] == pytest.approx([104.0, 108.0])
    assert [row["qty"] for row in take_profits] == pytest.approx([0.05, 0.05])


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


async def test_close_position_force_bypasses_slippage_guard(settings):
    client = WideCloseSlippageClient()
    ex = Executor(client, settings)
    pos = {
        "symbol": "BTC/USDT:USDT",
        "side": "short",
        "contracts": 0.05,
        "entryPrice": 101.0,
        "markPrice": 100.0,
    }

    rejected = await ex.close_position(pos)

    assert rejected["status"] == "rejected"
    assert rejected["raw"]["reason"] == "slippage_exceeded"
    assert client.created == []

    forced = await ex.close_position(pos, skip_slippage_guard=True)

    assert forced["closed"] is True
    assert forced["side"] == "buy"
    assert len(client.created) == 1
    assert client.created[0][0:4] == ("BTCUSDT", "buy", 0.05, "market")
    assert client.created[0][4]["reduceOnly"] is True


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
        self.amount_by_order_id: dict[str, float] = {}

    async def fetch_order_book(self, symbol, limit=5):
        return {"bids": [[100.0, 10]], "asks": [[100.2, 10]]}

    async def create_order(self, symbol, side, amount, order_type="market", price=None, params=None):
        self.created.append((symbol, side, amount, order_type, price, params))
        order_id = f"maker-{len(self.created)}"
        self.amount_by_order_id[order_id] = float(amount)
        return {
            "id": order_id,
            "price": price,
            "filled": 0.0,
            "status": "open",
            "info": {"orderId": order_id, "clientOrderId": params["newClientOrderId"]},
        }

    async def fetch_order(self, symbol, order_id, params=None):
        filled = min(0.02, self.amount_by_order_id.get(order_id, 0.02))
        return {
            "id": order_id,
            "price": 100.0,
            "average": 100.0,
            "filled": filled,
            "status": "open",
            "info": {"status": "PARTIALLY_FILLED", "executedQty": str(filled)},
        }

    async def cancel_order(self, symbol, order_id, params=None):
        self.canceled.append((symbol, order_id))
        return {"id": order_id, "status": "canceled"}

    async def fetch_order_trades(self, symbol, order_id, limit=100):
        return [{"fee": {"cost": 0.001, "currency": "USDT"}, "takerOrMaker": "maker"}]


async def test_maker_open_partial_fill_cancels_rest(settings):
    """单次 attempt 内部分成交：撤剩余，返回 partial 状态。"""
    settings.execution.entry_mode = ExecutionMode.MAKER_FIRST
    settings.execution.maker_timeout_seconds = 0.05
    settings.execution.maker_poll_seconds = 0.01
    settings.execution.maker_max_requotes = 0  # 限制只发 1 次，模拟单 attempt 场景
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


async def test_maker_open_accumulates_partial_fills_across_attempts(settings):
    """B2 修复：跨 attempt 累计真实成交，达到计划量后提前收尾为 filled。"""
    settings.execution.entry_mode = ExecutionMode.MAKER_FIRST
    settings.execution.maker_timeout_seconds = 0.05
    settings.execution.maker_poll_seconds = 0.01
    settings.execution.maker_max_requotes = 4
    client = MakerPartialClient()
    client._f = SymbolFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("1"),
    )
    ex = Executor(client, settings)

    # 每次按剩余量重挂，最后一次只请求 0.01，累计正好 0.05 → filled
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)

    assert res["status"] == "filled"
    assert res["qty"] == pytest.approx(0.05)
    assert res["remaining_qty"] == pytest.approx(0.0)
    assert [row[2] for row in client.created] == pytest.approx([0.05, 0.03, 0.01])
    # 3 次 attempt 后累计达到计划量，应该停止重试，不能再用原始数量重挂
    assert len(client.created) == 3


async def test_maker_open_partial_cumulative_below_target_returns_partial(settings):
    """跨 attempt 累计成交仍不足计划量时返回 partial。"""
    settings.execution.entry_mode = ExecutionMode.MAKER_FIRST
    settings.execution.maker_timeout_seconds = 0.05
    settings.execution.maker_poll_seconds = 0.01
    settings.execution.maker_max_requotes = 2  # 共 3 次 attempt
    client = MakerPartialClient()  # 每次 0.02
    ex = Executor(client, settings)

    res = await ex.open_position(decision=_decision(), qty=0.10, price=100.0)

    assert res["status"] == "partial"
    assert res["qty"] == pytest.approx(0.06)  # 3 × 0.02
    assert res["remaining_qty"] == pytest.approx(0.04)
    assert [row[2] for row in client.created] == pytest.approx([0.10, 0.08, 0.06])


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



class MakerNotFoundThenFilledClient(MakerPartialClient):
    """B1 修复：fetch_order 抛 OrderNotFound 时回退到 myTrades 拿到成交。"""

    def __init__(self):
        super().__init__()
        self.fetch_order_calls = 0

    async def fetch_order(self, symbol, order_id, params=None):
        self.fetch_order_calls += 1
        # 模拟订单被部分成交后被交易所自动取消，fetch_order 返回 -2013
        raise ccxt.OrderNotFound(f"{symbol} order {order_id} not found")


async def test_maker_wait_fill_recovers_via_my_trades_on_order_not_found(settings):
    """B1 修复核心测试：fetch_order 抛 OrderNotFound 时，回退到 myTrades 拿真实成交。"""
    settings.execution.entry_mode = ExecutionMode.MAKER_ONLY
    settings.execution.maker_timeout_seconds = 0.05
    settings.execution.maker_poll_seconds = 0.01
    settings.execution.maker_max_requotes = 0
    client = MakerNotFoundThenFilledClient()
    # 准备 myTrades 返回值：0.03 @ 100
    client._mytrades = [
        {"amount": 0.03, "price": 100.0, "fee": {"cost": 0.001, "currency": "USDT"},
         "takerOrMaker": "maker"},
    ]
    original_fetch_order_trades = client.fetch_order_trades
    async def _ft(symbol, order_id, limit=100):
        return client._mytrades
    client.fetch_order_trades = _ft  # type: ignore[assignment]
    ex = Executor(client, settings)

    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)

    # 关键断言：0.03 成交被恢复，整体返回 partial
    assert res["status"] == "partial"
    assert res["qty"] == pytest.approx(0.03)
    assert res["filled"] is True
    assert res["opened"] is True
    assert client.fetch_order_calls >= 1


class MakerNotFoundNoTradesClient(MakerPartialClient):
    """fetch_order 抛 OrderNotFound 且 myTrades 也无成交：走"未成交"分支。"""

    def __init__(self):
        super().__init__()
        self.fetch_order_calls = 0

    async def fetch_order(self, symbol, order_id, params=None):
        self.fetch_order_calls += 1
        raise ccxt.OrderNotFound(f"{symbol} order {order_id} not found")

    async def fetch_order_trades(self, symbol, order_id, limit=100):
        return []


async def test_maker_wait_fill_no_mytrades_treats_as_unfilled(settings):
    """B1：fetch_order 失败且 myTrades 无成交 → 走"未成交"，不臆造成交。"""
    settings.execution.entry_mode = ExecutionMode.MAKER_ONLY
    settings.execution.maker_timeout_seconds = 0.05
    settings.execution.maker_poll_seconds = 0.01
    settings.execution.maker_max_requotes = 0
    client = MakerNotFoundNoTradesClient()
    ex = Executor(client, settings)

    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)

    assert res["status"] == "canceled"
    assert res["filled"] is False
    assert res["opened"] is False


class MakerResidualReconcileClient(MakerUnfilledClient):
    """B3 测试：所有 attempt 内 fetch_order 都报 0 fill，但收尾时 myTrades 兜底查到成交。"""

    def __init__(self):
        super().__init__()
        self.attempt_count = 0
        self.mytrades_calls: list[str] = []

    async def create_order(self, symbol, side, amount, order_type="market", price=None, params=None):
        self.attempt_count += 1
        order_id = f"maker-{self.attempt_count}"
        self.created.append((symbol, side, amount, order_type, price, params))
        return {
            "id": order_id, "price": price, "filled": 0.0, "status": "open",
            "info": {"orderId": order_id, "clientOrderId": params["newClientOrderId"]},
        }

    async def fetch_order_trades(self, symbol, order_id, limit=100):
        self.mytrades_calls.append(order_id)
        # 只对第一单返回成交
        if order_id == "maker-1":
            return [{"amount": 0.025, "price": 100.0,
                     "fee": {"cost": 0.0008, "currency": "USDT"}}]
        return []


async def test_maker_residual_mytrades_reconcile_picks_up_orphan_fills(settings):
    """B3 修复核心测试：所有 attempt 内 fetch_order 报 0 fill，
    收尾时通过 myTrades 兜底查到第一单的 0.025 成交。
    """
    settings.execution.entry_mode = ExecutionMode.MAKER_ONLY
    settings.execution.maker_timeout_seconds = 0.02
    settings.execution.maker_poll_seconds = 0.005
    settings.execution.maker_max_requotes = 2  # 3 次 attempt
    client = MakerResidualReconcileClient()
    ex = Executor(client, settings)

    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)

    # 收尾 myTrades 兜底拿到 0.025，返回 partial
    assert res["status"] == "partial"
    assert res["qty"] == pytest.approx(0.025)
    assert res["filled"] is True
    # 收尾时应该查过所有 attempt 的 order_id
    assert "maker-1" in client.mytrades_calls


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



class MakerHardCapClient(MakerPartialClient):
    """C1：fetch_order 持续抛瞬时错误，触发 maker wait 硬上限。"""

    def __init__(self):
        super().__init__()
        self.fetch_order_calls = 0

    async def fetch_order(self, symbol, order_id, params=None):
        self.fetch_order_calls += 1
        # 模拟 ccxt RateLimitExceeded，触发 _wait_maker_fill 的 transient retry + hard cap
        raise ccxt.RateLimitExceeded("simulated rate limit")


async def test_maker_wait_hits_hard_cap_when_fetch_order_hangs(settings):
    """C1：fetch_order 一直抛瞬时错误，maker wait 不应卡死，超出 hard_cap 后中止。"""
    settings.execution.entry_mode = ExecutionMode.MAKER_ONLY
    settings.execution.maker_timeout_seconds = 0.05
    settings.execution.maker_poll_seconds = 0.01
    settings.execution.maker_max_requotes = 0
    client = MakerHardCapClient()
    ex = Executor(client, settings)

    t0 = time.monotonic()
    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)
    elapsed = time.monotonic() - t0

    # 硬上限 = max(timeout*2, 5s) = max(0.1, 5) = 5s。允许 6s 余量。
    assert elapsed < 6.0, f"hard cap should abort <6s, got {elapsed:.2f}s"
    # hard cap abort 后 status=canceled (与原 unfilled 行为一致)
    assert res["status"] == "canceled"
    assert res["filled"] is False


async def test_recover_via_mytrades_aggregates_multiple_trades(settings):
    """B1 + B3 边界：单 order 多笔成交（部分成交分批返回），加权重计算正确。"""
    settings.execution.entry_mode = ExecutionMode.MAKER_ONLY
    settings.execution.maker_timeout_seconds = 0.05
    settings.execution.maker_poll_seconds = 0.01
    settings.execution.maker_max_requotes = 0

    class _MyTradesSplitClient(MakerNotFoundThenFilledClient):
        def __init__(self):
            super().__init__()
            # 3 笔成交：0.01 @ 100, 0.015 @ 100.5, 0.005 @ 99.5
            self._mytrades = [
                {"amount": 0.01, "price": 100.0, "fee": {"cost": 0.0001, "currency": "USDT"}},
                {"amount": 0.015, "price": 100.5, "fee": {"cost": 0.0002, "currency": "USDT"}},
                {"amount": 0.005, "price": 99.5, "fee": {"cost": 0.0001, "currency": "USDT"}},
            ]
        async def fetch_order_trades(self, symbol, order_id, limit=100):
            return self._mytrades
    client = _MyTradesSplitClient()
    ex = Executor(client, settings)

    res = await ex.open_position(decision=_decision(), qty=0.05, price=100.0)

    # 累加 0.01 + 0.015 + 0.005 = 0.03 < 0.05 → partial
    assert res["status"] == "partial"
    assert res["qty"] == pytest.approx(0.03)
    # 加权均价 = (0.01*100 + 0.015*100.5 + 0.005*99.5) / 0.03
    expected_avg = (0.01 * 100.0 + 0.015 * 100.5 + 0.005 * 99.5) / 0.03
    assert res["price"] == pytest.approx(expected_avg, rel=1e-6)
    # 手续费累加
    assert res["fee"] == pytest.approx(0.0004)
