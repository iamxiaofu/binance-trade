from __future__ import annotations

import time

from sqlalchemy import text

from src.exchange.events import ExchangeEvent, rest_snapshot_event
from src.state.account import AccountStateCoordinator
from src.state.runtime import RuntimeState
from src.store.repo import Store


async def _coordinator(tmp_path):
    store = Store(str(tmp_path / "account.db"))
    await store.connect()
    runtime = RuntimeState()
    coordinator = AccountStateCoordinator(store, runtime)
    await coordinator.start()
    return store, runtime, coordinator


async def test_rest_baseline_and_partial_account_update_do_not_clear_other_positions(tmp_path):
    store, runtime, coordinator = await _coordinator(tmp_path)
    try:
        await coordinator.submit(rest_snapshot_event(
            positions=[
                {"symbol": "BTC/USDT:USDT", "contracts": 1, "side": "long", "entryPrice": 100},
                {"symbol": "ETH/USDT:USDT", "contracts": 2, "side": "long", "entryPrice": 200},
            ],
            open_orders=[],
            balance={"total": {"USDT": 1000}, "free": {"USDT": 900}},
            reason="test",
        ))
        await coordinator.drain()
        event = ExchangeEvent(
            event_type="ACCOUNT_UPDATE",
            event_time_ms=10,
            transaction_time_ms=10,
            payload={"a": {"P": [{
                "s": "BTCUSDT", "pa": "0", "ep": "0", "up": "0",
                "mt": "isolated", "iw": "0", "ps": "BOTH",
            }]}},
            event_key="partial-update",
        )
        await coordinator.submit(event)
        await coordinator.drain()
        assert "BTCUSDT" not in runtime.positions
        assert "ETHUSDT" in runtime.positions
    finally:
        await coordinator.close()
        await store.close()


async def test_duplicate_and_stale_private_events_are_idempotent(tmp_path):
    store, runtime, coordinator = await _coordinator(tmp_path)
    try:
        fresh = ExchangeEvent(
            event_type="ACCOUNT_UPDATE",
            transaction_time_ms=200,
            payload={"a": {"P": [{
                "s": "BTCUSDT", "pa": "1", "ep": "100", "up": "0",
                "mt": "isolated", "iw": "20", "ps": "BOTH",
            }]}},
            event_key="fresh",
        )
        stale = ExchangeEvent(
            event_type="ACCOUNT_UPDATE",
            transaction_time_ms=100,
            payload={"a": {"P": [{
                "s": "BTCUSDT", "pa": "0", "ep": "0", "up": "0",
                "mt": "isolated", "iw": "0", "ps": "BOTH",
            }]}},
            event_key="stale",
        )
        await coordinator.submit(fresh)
        await coordinator.submit(fresh)
        await coordinator.submit(stale)
        await coordinator.drain()
        assert "BTCUSDT" in runtime.positions
        async with store._sessionmaker() as session:
            rows = (await session.execute(
                text("SELECT COUNT(*) FROM exchange_events")
            )).scalar_one()
        assert rows == 2
    finally:
        await coordinator.close()
        await store.close()


async def test_order_update_marks_strategy_wake_event(tmp_path):
    store, runtime, coordinator = await _coordinator(tmp_path)
    try:
        now = int(time.time() * 1000)
        await coordinator.submit(ExchangeEvent(
            event_type="ORDER_TRADE_UPDATE",
            transaction_time_ms=now,
            payload={"o": {
                "i": 123, "s": "BTCUSDT", "S": "BUY", "o": "LIMIT",
                "q": "1", "p": "100", "z": "1", "ap": "100", "X": "FILLED",
                "f": "GTC", "R": False, "c": "bt-test",
            }},
            event_key="order-filled",
        ))
        await coordinator.drain()
        assert runtime.pop_order_event("BTCUSDT") is True
    finally:
        await coordinator.close()
        await store.close()


async def test_algo_update_projects_tp_trigger_price(tmp_path):
    store, runtime, coordinator = await _coordinator(tmp_path)
    try:
        now = int(time.time() * 1000)
        await coordinator.submit(ExchangeEvent(
            event_type="ALGO_UPDATE",
            transaction_time_ms=now,
            payload={"o": {
                "aid": 2000001144654254,
                "caid": "ios-example",
                "s": "SOLUSDT",
                "S": "SELL",
                "o": "TAKE_PROFIT_MARKET",
                "q": "4.51",
                "p": "0",
                "tp": "72.23",
                "X": "NEW",
                "R": True,
            }},
            event_key="algo-tp-trigger",
        ))
        await coordinator.drain()
        order = runtime.open_orders["SOLUSDT"][0]
        assert order["status"] == "placed"
        assert order["price"] == 0
        assert order["trigger_price"] == 72.23
        live = await store.live_account_state()
        assert live["open_orders"][0]["trigger_price"] == 72.23
    finally:
        await coordinator.close()
        await store.close()


async def test_account_update_preserves_rest_margin_fields_and_updates_isolated_wallet(tmp_path):
    store, runtime, coordinator = await _coordinator(tmp_path)
    try:
        baseline = rest_snapshot_event(
            positions=[{
                "symbol": "SOL/USDT:USDT",
                "side": "long",
                "contracts": 4.51,
                "entryPrice": 71.13,
                "markPrice": 71.20,
                "notional": 321.112,
                "initialMargin": 126.10,
                "collateral": 64.45,
                "isolatedWallet": 64.09,
                "liquidationPrice": 50.0,
                "marginMode": "isolated",
            }],
            open_orders=[],
            balance={"total": {"USDT": 1000}, "free": {"USDT": 900}},
            reason="margin-baseline",
        )
        await coordinator.submit(baseline)
        await coordinator.drain()

        await coordinator.submit(ExchangeEvent(
            event_type="ACCOUNT_UPDATE",
            event_time_ms=baseline.event_time_ms + 1,
            transaction_time_ms=baseline.transaction_time_ms + 1,
            payload={"a": {"P": [{
                "s": "SOLUSDT",
                "pa": "4.51",
                "ep": "71.13",
                "up": "0.50",
                "mt": "isolated",
                "iw": "64.20",
                "ps": "BOTH",
            }]}},
            event_key="isolated-wallet-update",
        ))
        await coordinator.drain()

        live = await store.live_account_state()
        position = live["positions"][0]
        assert position["source"] == "stream"
        assert position["isolated_wallet"] == 64.20
        assert position["unrealized_pnl"] == 0.50
        assert position["isolated_margin"] == 64.45
        assert position["initial_margin"] == 126.10
        assert position["liquidation_price"] == 50.0
    finally:
        await coordinator.close()
        await store.close()
