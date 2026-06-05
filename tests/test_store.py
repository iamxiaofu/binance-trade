"""store (models+repo) 测试：真实临时 SQLite，验证落库与对账。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.llm.schema import Action, TradeDecision
from src.risk.manager import RejectCode, Verdict
from src.state.runtime import RuntimeState
from src.store.models import (
    BalanceSnapshotRow,
    DecisionRow,
    OpenOrderRow,
    OrderRow,
    PositionSnapshotRow,
    RejectRow,
    RuntimeSettingRow,
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
    await store.log_decision(symbol="BTCUSDT", decision=d, ref_price=100.0)
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        row = (await session.execute(select(DecisionRow))).scalar_one()
    assert row.action == "OPEN_LONG"
    assert row.leverage == 3
    assert row.skipped is False


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
        "qty": 0.01, "price": 100.0, "notional": 1.0, "dry_run": True,
        "status": "dry_run", "id": "", "raw": {"a": 1},
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


async def test_runtime_settings_upsert_and_list(store):
    assert await store.get_runtime_setting("execution.dry_run") is None
    await store.set_runtime_setting("execution.dry_run", "true")
    await store.set_runtime_setting("execution.dry_run", "false")

    assert await _count(store, RuntimeSettingRow) == 1
    assert await store.get_runtime_setting("execution.dry_run") == "false"
    assert await store.runtime_settings() == {"execution.dry_run": "false"}


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
    cid = await store.enqueue_command("SET_DRY_RUN", arg="false")
    await store.mark_command(cid, "done", "dry_run set to False")
    assert await store.fetch_pending_commands() == []
    recent = await store.recent_commands()
    assert recent[0]["status"] == "done"
    assert recent[0]["arg"] == "false"


async def test_command_fifo_order(store):
    a = await store.enqueue_command("PAUSE")
    b = await store.enqueue_command("RESUME")
    pending = await store.fetch_pending_commands()
    assert [p["id"] for p in pending] == [a, b]  # 先进先出
