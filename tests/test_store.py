"""store (models+repo) 测试：真实临时 SQLite，验证落库与对账。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.llm.schema import Action, TradeDecision
from src.risk.manager import RejectCode, Verdict
from src.state.runtime import RuntimeState
from src.exchange.filters import SymbolFilters
from src.store.models import (
    BalanceSnapshotRow,
    DecisionRow,
    OpenOrderRow,
    OrderRow,
    PositionClaimRow,
    PositionSnapshotRow,
    RejectRow,
    RuntimeSettingRow,
    SymbolRow,
    TradeRow,
)
from src.store.repo import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    await s.connect()
    yield s
    await s.close()


async def _count(store: Store, model) -> int:
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_log_skipped_decision(store):
    await store.log_decision(symbol="BTCUSDT", skipped=True, skip_reason="no change", ref_price=100.0)
    assert await _count(store, DecisionRow) == 1


async def test_log_actual_decision_with_context(store):
    d = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.8,
                      size_pct=0.1, leverage=3, stop_loss_pct=0.02,
                      take_profit_pct=0.04, reason="trend up")
    await store.log_decision(
        symbol="BTCUSDT",
        decision=d,
        ref_price=100.0,
        llm_prompt="prompt",
        llm_request_json='{"request": true}',
        llm_response_json='{"response": true}',
        feature_snapshot_json='{"last_price": 100.0}',
    )
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        row = (await session.execute(select(DecisionRow))).scalar_one()
    assert row.action == "OPEN_LONG"
    assert row.leverage == 3
    assert row.skipped is False
    assert row.llm_prompt == "prompt"
    assert "request" in row.llm_request_json
    assert "response" in row.llm_response_json
    assert "last_price" in row.feature_snapshot_json


async def test_latest_decision_snapshot(store):
    d = TradeDecision(symbol="BTCUSDT", action=Action.HOLD, confidence=0.8,
                      size_pct=0.0, leverage=1, stop_loss_pct=0.0,
                      take_profit_pct=0.0, reason="wait")
    await store.log_decision(
        symbol="BTCUSDT",
        decision=d,
        ref_price=100.0,
        feature_snapshot_json='{"symbol": "BTCUSDT", "last_price": 100.0}',
    )
    snap = await store.latest_decision_snapshot("BTCUSDT")
    assert snap is not None
    assert snap["snapshot"]["last_price"] == 100.0


async def test_log_reject(store):
    d = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.8,
                      size_pct=0.1, leverage=99, stop_loss_pct=0.02,
                      take_profit_pct=0.04, reason="x")
    v = Verdict(passed=False, code=RejectCode.LEVERAGE_EXCEEDED, reason="lev too high")
    await store.log_reject(symbol="BTCUSDT", verdict=v, decision=d)
    assert await _count(store, RejectRow) == 1


async def test_log_order_and_snapshots(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy", "order_type": "market",
        "qty": 0.01, "price": 100.0, "notional": 1.0, "dry_run": False,
        "status": "filled", "id": "open", "raw": {"a": 1},
    })
    assert await _count(store, OrderRow) == 1

    positions = [{
        "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.01,
        "entryPrice": 100.0, "markPrice": 101.0, "leverage": 3, "unrealizedPnl": 0.01,
    }]
    await store.snapshot_positions(positions)
    assert await _count(store, PositionSnapshotRow) == 1

    rt = RuntimeState()
    rt.day_realized_pnl = -5.0
    await store.snapshot_balance(total_equity=200.0, available_margin=180.0, runtime=rt)
    assert await _count(store, BalanceSnapshotRow) == 1


async def test_log_order_groups_short_trade_with_protection_and_close(store):
    d = TradeDecision(symbol="ETHUSDT", action=Action.OPEN_SHORT, confidence=0.8,
                      size_pct=0.1, leverage=5, stop_loss_pct=0.02,
                      take_profit_pct=0.04, reason="trend down")
    await store.log_decision(symbol="ETHUSDT", decision=d, ref_price=100.0)
    opened = await store.log_order({
        "symbol": "ETHUSDT", "kind": "OPEN", "side": "sell",
        "order_type": "market", "qty": 2.0, "price": 100.0,
        "notional": 200.0, "dry_run": False, "status": "filled",
        "id": "open", "raw": {}, "leverage": 5,
    })
    await store.log_order({
        "symbol": "ETHUSDT", "kind": "SL", "side": "buy",
        "order_type": "STOP_MARKET", "qty": 2.0, "price": 105.0,
        "notional": 210.0, "dry_run": False, "status": "placed",
        "id": "sl", "raw": {}, "trade_id": opened["trade_id"],
    })
    await store.log_order({
        "symbol": "ETHUSDT", "kind": "TP", "side": "buy",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 2.0, "price": 95.0,
        "notional": 190.0, "dry_run": False, "status": "placed",
        "id": "tp", "raw": {}, "trade_id": opened["trade_id"],
    })
    await store.log_order({
        "symbol": "ETHUSDT", "kind": "CLOSE", "side": "buy",
        "order_type": "market", "qty": 2.0, "price": 96.0,
        "notional": 192.0, "dry_run": False, "status": "filled",
        "id": "close", "raw": {},
    })

    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
        orders = (await session.execute(select(OrderRow).order_by(OrderRow.id))).scalars().all()

    assert trade.direction == "short"
    assert trade.status == "closed"
    assert trade.leverage == 5
    assert trade.entry_margin == pytest.approx(40.0)
    assert trade.realized_pnl == pytest.approx(8.0)
    assert trade.pnl_pct_on_margin == pytest.approx(20.0)
    assert trade.exit_reason == "CLOSE"
    assert {row.trade_id for row in orders} == {trade.id}
    assert [row.trade_role for row in orders] == ["ENTRY", "PROTECTION_SL", "PROTECTION_TP", "EXIT"]


async def test_partial_close_keeps_trade_partial_until_remaining_closes(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "limit", "qty": 1.0, "price": 100.0,
        "notional": 100.0, "dry_run": False, "status": "filled",
        "id": "open", "raw": {}, "leverage": 2, "fee": 0.02,
        "liquidity": "maker",
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "CLOSE", "side": "sell",
        "order_type": "limit", "qty": 0.4, "price": 110.0,
        "notional": 44.0, "dry_run": False, "status": "partial",
        "id": "close-1", "raw": {}, "fee": 0.01, "liquidity": "maker",
    })

    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
    assert trade.status == "partial"
    assert trade.qty_closed == pytest.approx(0.4)
    assert trade.realized_pnl == pytest.approx(4.0)
    assert trade.net_realized_pnl == pytest.approx(3.97)
    assert trade.entry_liquidity == "maker"
    assert trade.exit_liquidity == "maker"

    await store.log_order({
        "symbol": "BTCUSDT", "kind": "CLOSE", "side": "sell",
        "order_type": "market", "qty": 0.6, "price": 108.0,
        "notional": 64.8, "dry_run": False, "status": "filled",
        "id": "close-2", "raw": {}, "fee": 0.02, "liquidity": "taker",
    })

    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
    assert trade.status == "closed"
    assert trade.qty_closed == pytest.approx(1.0)
    assert trade.realized_pnl == pytest.approx(8.8)
    assert trade.total_fee == pytest.approx(0.05)
    assert trade.net_realized_pnl == pytest.approx(8.75)


async def test_aggregate_close_allocates_across_open_trades_fifo(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "limit", "qty": 0.08, "price": 100.0,
        "notional": 8.0, "dry_run": False, "status": "filled",
        "id": "open-1", "raw": {},
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "limit", "qty": 0.02, "price": 100.0,
        "notional": 2.0, "dry_run": False, "status": "filled",
        "id": "open-2", "raw": {},
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "CLOSE", "side": "sell",
        "order_type": "market", "qty": 0.1, "price": 90.0,
        "notional": 9.0, "dry_run": False, "status": "filled",
        "id": "close-all", "raw": {},
    })

    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trades = (await session.execute(select(TradeRow).order_by(TradeRow.id))).scalars().all()
        close = (
            await session.execute(
                select(OrderRow).where(OrderRow.client_kind == "CLOSE")
            )
        ).scalar_one()

    assert [trade.status for trade in trades] == ["closed", "closed"]
    assert [trade.qty_closed for trade in trades] == pytest.approx([0.08, 0.02])
    assert [trade.realized_pnl for trade in trades] == pytest.approx([-0.8, -0.2])
    assert close.realized_pnl == pytest.approx(-1.0)


async def test_reconcile_symbol_flat_closes_orphan_open_trade(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "limit", "qty": 0.1, "price": 100.0,
        "notional": 10.0, "dry_run": False, "status": "filled",
        "id": "open-flat", "raw": {},
    })

    changed = await store.reconcile_symbol_flat("BTCUSDT", reason="EXCHANGE_FLAT")

    assert changed == 1
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
    assert trade.status == "closed"
    assert trade.qty_closed == pytest.approx(0.1)
    assert trade.exit_reason == "EXCHANGE_FLAT"
    assert trade.confidence == "inferred"


async def test_has_open_trade_detects_local_managed_position(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "market", "qty": 0.01, "price": 100.0,
        "notional": 1.0, "dry_run": False, "status": "filled",
        "id": "open-managed", "raw": {},
    })

    assert await store.has_open_trade("BTCUSDT") is True
    assert await store.has_open_trade("ETHUSDT") is False


async def test_position_claim_lifecycle(store):
    claim_id = await store.begin_position_claim(
        symbol="BTCUSDT",
        side="long",
        planned_qty=0.01,
        ttl_ms=60_000,
        reason="test",
    )

    assert await store.has_active_position_claim("BTCUSDT") is True

    await store.finish_position_claim(
        claim_id,
        status="partial",
        filled_qty=0.01,
        entry_price=100.0,
        client_order_id="open-1",
    )

    assert await store.has_active_position_claim("BTCUSDT") is False
    assert await _count(store, PositionClaimRow) == 1


async def test_mark_condition_exit_closes_group_with_filled_price(store):
    opened = await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "market", "qty": 1.0, "price": 100.0,
        "notional": 100.0, "dry_run": False, "status": "filled",
        "id": "open", "raw": {}, "leverage": 2,
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "SL", "side": "sell",
        "order_type": "STOP_MARKET", "qty": 1.0, "price": 95.0,
        "notional": 95.0, "dry_run": False, "status": "placed",
        "id": "sl", "raw": {}, "trade_id": opened["trade_id"],
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "TP", "side": "sell",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 1.0, "price": 110.0,
        "notional": 110.0, "dry_run": False, "status": "placed",
        "id": "tp", "raw": {}, "trade_id": opened["trade_id"],
    })

    await store.mark_condition_exit(symbol="BTCUSDT", triggered_kind="TP", qty=1.0, price=109.5)

    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
        rows = (await session.execute(select(OrderRow).order_by(OrderRow.id))).scalars().all()
    by_kind = {row.client_kind: row for row in rows}

    assert trade.status == "closed"
    assert trade.exit_reason == "TP"
    assert trade.exit_price == pytest.approx(109.5)
    assert trade.realized_pnl == pytest.approx(9.5)
    assert by_kind["TP"].realized_pnl == pytest.approx(9.5)
    assert by_kind["SL"].status == "canceled"


async def test_sync_condition_history_marks_triggered_stop_while_position_remains(store):
    opened = await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "limit", "qty": 0.08, "price": 100.0,
        "notional": 8.0, "dry_run": False, "status": "partial",
        "id": "open", "raw": {}, "leverage": 2,
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "SL", "side": "sell",
        "order_type": "STOP_MARKET", "qty": 0.08, "price": 99.0,
        "notional": 7.92, "dry_run": False, "status": "placed",
        "id": "sl", "raw": {}, "trade_id": opened["trade_id"],
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "TP", "side": "sell",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 0.08, "price": 103.0,
        "notional": 8.24, "dry_run": False, "status": "placed",
        "id": "tp", "raw": {}, "trade_id": opened["trade_id"],
    })

    changed = await store.sync_condition_order_history(
        symbol="BTCUSDT",
        live_exchange_order_ids={"tp"},
        history_orders=[
            {
                "id": "sl",
                "symbol": "BTCUSDT",
                "kind": "SL",
                "status": "filled",
                "qty": 0.08,
                "filled_qty": 0.08,
                "filled_price": 98.8,
                "trigger_price": 99.0,
            }
        ],
    )

    assert changed == 1
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
        rows = (await session.execute(select(OrderRow).order_by(OrderRow.id))).scalars().all()
    by_kind = {row.client_kind: row for row in rows}
    assert trade.status == "closed"
    assert trade.exit_reason == "SL"
    assert by_kind["SL"].status == "filled"
    assert by_kind["TP"].status == "placed"


async def test_runtime_settings_upsert_and_list(store):
    assert await store.get_runtime_setting("strategy.paused") is None
    await store.set_runtime_setting("strategy.paused", "true")
    await store.set_runtime_setting("strategy.paused", "false")

    assert await _count(store, RuntimeSettingRow) == 1
    assert await store.get_runtime_setting("strategy.paused") == "false"
    assert await store.runtime_settings() == {"strategy.paused": "false"}


async def test_runtime_settings_batch_upsert(store):
    await store.set_runtime_settings({
        "strategy.paused": "true",
        "symbol.enabled.BTCUSDT": "false",
    })
    await store.set_runtime_settings({
        "strategy.paused": "false",
        "symbol.enabled.ETHUSDT": "true",
    })

    assert await _count(store, RuntimeSettingRow) == 3
    assert await store.runtime_settings() == {
        "strategy.paused": "false",
        "symbol.enabled.BTCUSDT": "false",
        "symbol.enabled.ETHUSDT": "true",
    }


async def test_sync_config_symbols_seeds_registry_from_runtime_setting(store):
    await store.set_runtime_setting("symbol.enabled.BTCUSDT", "false")
    await store.sync_config_symbols(["BTCUSDT", "ETHUSDT"])

    rows = {row["symbol"]: row for row in await store.list_symbols()}
    assert rows["BTCUSDT"]["source"] == "config"
    assert rows["BTCUSDT"]["enabled"] is False
    assert rows["ETHUSDT"]["enabled"] is True
    assert await _count(store, SymbolRow) == 2


async def test_upsert_dynamic_symbol_from_exchange_defaults_disabled(store):
    filters = SymbolFilters(
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
    )
    row = await store.upsert_symbol_from_exchange(
        symbol="solusdt",
        filters=filters,
        exchange_state={"position": {}, "open_orders": [], "condition_orders": []},
        sync_status="confirmed_flat",
        needs_review=False,
    )

    assert row["symbol"] == "SOLUSDT"
    assert row["enabled"] is False
    assert row["sync_status"] == "confirmed_flat"
    assert row["min_notional"] == 5.0
    assert await store.get_runtime_setting("symbol.enabled.SOLUSDT") == "false"


async def test_set_symbol_enabled_updates_registry_and_runtime(store):
    await store.sync_config_symbols(["BTCUSDT"])
    await store.set_symbol_enabled("BTCUSDT", False)

    row = await store.get_symbol("BTCUSDT")
    assert row["enabled"] is False
    assert await store.get_runtime_setting("symbol.enabled.BTCUSDT") == "false"


async def test_update_symbol_filters_keeps_enabled_state(store):
    await store.set_runtime_setting("symbol.enabled.BTCUSDT", "false")
    await store.sync_config_symbols(["BTCUSDT"])
    filters = SymbolFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("10"),
    )

    await store.update_symbol_filters("BTCUSDT", filters)

    row = await store.get_symbol("BTCUSDT")
    assert row["enabled"] is False
    assert row["tick_size"] == 0.1
    assert row["min_notional"] == 10.0


async def test_mark_condition_exit_marks_triggered_and_cancels_other(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "SL", "side": "buy", "order_type": "STOP_MARKET",
        "qty": 0.01, "price": 105.0, "notional": 1.05, "dry_run": False,
        "status": "placed", "id": "sl", "raw": {},
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "TP", "side": "buy",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 0.01, "price": 95.0,
        "notional": 0.95, "dry_run": False, "status": "placed", "id": "tp", "raw": {},
    })
    await store.mark_condition_exit(symbol="BTCUSDT", triggered_kind="TP", qty=0.01, price=94.5)
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        rows = (await session.execute(select(OrderRow).order_by(OrderRow.id))).scalars().all()
    by_kind = {r.client_kind: r for r in rows}
    assert by_kind["TP"].status == "filled"
    assert by_kind["TP"].price == pytest.approx(95.0)
    raw = json.loads(by_kind["TP"].raw_json)
    assert raw["trigger_price"] == pytest.approx(95.0)
    assert raw["filled_price"] == pytest.approx(94.5)
    assert by_kind["SL"].status == "canceled"


async def test_latest_protection_templates_returns_latest_sl_tp(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "SL", "side": "sell", "order_type": "STOP_MARKET",
        "qty": 0.01, "price": 98.0, "notional": 0.98, "dry_run": False,
        "status": "canceled", "id": "old-sl", "raw": {},
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "SL", "side": "sell", "order_type": "STOP_MARKET",
        "qty": 0.01, "price": 97.0, "notional": 0.97, "dry_run": False,
        "status": "placed", "id": "new-sl", "raw": {},
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "TP", "side": "sell",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 0.01, "price": 104.0,
        "notional": 1.04, "dry_run": False, "status": "canceled", "id": "tp", "raw": {},
    })

    templates = await store.latest_protection_templates("BTCUSDT", dry_run=False)

    assert templates["SL"]["price"] == pytest.approx(97.0)
    assert templates["TP"]["price"] == pytest.approx(104.0)


async def test_mark_orders_status_by_exchange_ids(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "TP", "side": "sell",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 0.01, "price": 104.0,
        "notional": 1.04, "dry_run": False, "status": "placed", "id": "tp", "raw": {},
    })

    changed = await store.mark_orders_status_by_exchange_ids({"tp"}, "canceled")

    assert changed == 1
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        row = (await session.execute(select(OrderRow))).scalar_one()
    assert row.status == "canceled"


async def test_mark_symbol_conditions_not_live(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "SL", "side": "sell",
        "order_type": "STOP_MARKET", "qty": 0.01, "price": 98.0,
        "notional": 0.98, "dry_run": False, "status": "placed",
        "id": "live-sl", "raw": {},
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "TP", "side": "sell",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 0.01, "price": 104.0,
        "notional": 1.04, "dry_run": False, "status": "placed",
        "id": "gone-tp", "raw": {},
    })

    changed = await store.mark_symbol_conditions_not_live("BTCUSDT", {"live-sl"})

    assert changed == 1
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        rows = (await session.execute(select(OrderRow))).scalars().all()
    by_id = {row.exchange_order_id: row for row in rows}
    assert by_id["live-sl"].status == "placed"
    assert by_id["gone-tp"].status == "canceled"


async def test_snapshot_skips_zero_contracts(store):
    await store.snapshot_positions([{"symbol": "BTC/USDT:USDT", "contracts": 0}])
    assert await _count(store, PositionSnapshotRow) == 0


async def test_snapshot_records_zero_for_tracked_symbols(store):
    await store.snapshot_positions(
        [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0}],
        symbols=["BTCUSDT"],
    )
    assert await _count(store, PositionSnapshotRow) == 1


async def test_reconcile_fills_runtime(store):
    rt = RuntimeState()
    positions = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.5, "markPrice": 100.0},
        {"symbol": "ETH/USDT:USDT", "side": "short", "contracts": 0},  # 应忽略
    ]
    await store.reconcile(positions, rt)
    assert "BTCUSDT" in rt.positions
    assert "ETHUSDT" not in rt.positions
    assert await _count(store, PositionSnapshotRow) == 1


async def test_reconcile_restores_open_orders(store):
    rt = RuntimeState()
    positions = [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.5, "markPrice": 100.0}]
    open_orders = [
        {"id": "1", "symbol": "BTC/USDT:USDT", "type": "stop_market", "side": "sell",
         "amount": 0.5, "price": None, "stopPrice": 98.0, "reduceOnly": True,
         "status": "open", "info": {"stopPrice": "98.0"}},
        {"id": "2", "symbol": "BTC/USDT:USDT", "type": "take_profit_market", "side": "sell",
         "amount": 0.5, "stopPrice": 104.0, "reduceOnly": True, "status": "open", "info": {}},
    ]
    await store.reconcile(positions, rt, open_orders)
    assert len(rt.open_orders.get("BTCUSDT", [])) == 2
    assert await _count(store, OpenOrderRow) == 2


async def test_reconcile_no_open_orders_ok(store):
    rt = RuntimeState()
    await store.reconcile([], rt, None)
    assert rt.open_orders == {}
    assert await _count(store, OpenOrderRow) == 0


# ---------- 控制命令队列 ----------
async def test_command_enqueue_and_fetch(store):
    cid = await store.enqueue_command("KILL_SWITCH", source="web")
    assert cid > 0
    pending = await store.fetch_pending_commands()
    assert len(pending) == 1
    assert pending[0]["command"] == "KILL_SWITCH"
    assert pending[0]["id"] == cid


async def test_command_mark_done_removes_from_pending(store):
    cid = await store.enqueue_command("SET_SYMBOL_ENABLED", arg="BTCUSDT=false")
    await store.mark_command(cid, "done", "BTCUSDT strategy enabled set to False")
    assert await store.fetch_pending_commands() == []
    recent = await store.recent_commands()
    assert recent[0]["status"] == "done"
    assert recent[0]["arg"] == "BTCUSDT=false"


async def test_command_fifo_order(store):
    a = await store.enqueue_command("PAUSE")
    b = await store.enqueue_command("RESUME")
    pending = await store.fetch_pending_commands()
    assert [p["id"] for p in pending] == [a, b]  # 先进先出
