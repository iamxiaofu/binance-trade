"""store (models+repo) 测试：真实临时 SQLite，验证落库与对账。"""
from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.llm.schema import DECISION_REASON_MAX_LENGTH, Action, TradeDecision
from src.risk.manager import RejectCode, Verdict
from src.state.runtime import RuntimeState
from src.exchange.filters import SymbolFilters
from src.store.models import (
    BalanceSnapshotRow,
    DecisionRow,
    ExchangeFillRow,
    ExternalTradeFillRow,
    ExternalTradeRow,
    OpenOrderRow,
    OrderRow,
    PositionClaimRow,
    PositionSnapshotRow,
    RejectRow,
    LLMPromptVersionRow,
    LivePositionRow,
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


async def test_connect_creates_read_query_indexes(store):
    async with store._engine.connect() as conn:
        decision_rows = (await conn.execute(text("PRAGMA index_list(decisions)"))).fetchall()
        trade_rows = (await conn.execute(text("PRAGMA index_list(trades)"))).fetchall()
        order_rows = (await conn.execute(text("PRAGMA index_list(orders)"))).fetchall()
    decision_names = {row[1] for row in decision_rows}
    trade_names = {row[1] for row in trade_rows}
    order_names = {row[1] for row in order_rows}
    assert "ix_decisions_ts_id" in decision_names
    assert "ix_decisions_symbol_ts_id" in decision_names
    assert "ix_trades_opened_id" in trade_names
    assert "ix_trades_symbol_opened_id" in trade_names
    assert "ix_trades_status_opened_id" in trade_names
    assert "ix_orders_trade_ts_id" in order_names


def _fill(
    trade_id: str,
    *,
    side: str,
    qty: float,
    price: float,
    ts_ms: int,
    client_order_id: str = "manual-order",
    realized_pnl: float = 0.0,
    fee: float = 0.01,
) -> dict:
    return {
        "symbol": "BTCUSDT",
        "exchange_trade_id": trade_id,
        "exchange_order_id": f"order-{trade_id}",
        "client_order_id": client_order_id,
        "side": side,
        "qty": qty,
        "price": price,
        "ts_ms": ts_ms,
        "realized_pnl": realized_pnl,
        "fee": fee,
        "fee_asset": "USDT",
        "liquidity": "taker",
        "source": "stream",
    }


async def test_engine_fill_is_ledgered_without_external_trade(store):
    result = await store.ingest_exchange_fill(
        _fill("1", side="buy", qty=0.1, price=100.0, ts_ms=1_000, client_order_id="bt-entry")
    )
    assert result["ownership"] == "engine"
    assert await _count(store, ExchangeFillRow) == 1
    assert await _count(store, ExternalTradeRow) == 0


async def test_external_fill_lifecycle_and_duplicate_are_isolated(store):
    opened = await store.ingest_exchange_fill(
        _fill("10", side="buy", qty=2.0, price=100.0, ts_ms=10_000)
    )
    assert opened["ownership"] == "external"
    duplicate = await store.ingest_exchange_fill(
        _fill("10", side="buy", qty=2.0, price=100.0, ts_ms=10_000)
    )
    assert duplicate["inserted"] is False

    await store.ingest_exchange_fill(
        _fill("11", side="sell", qty=0.5, price=110.0, ts_ms=11_000, realized_pnl=5.0)
    )
    await store.ingest_exchange_fill(
        _fill("12", side="sell", qty=1.5, price=90.0, ts_ms=12_000, realized_pnl=-15.0)
    )

    async with store._sessionmaker() as session:
        trade = (
            await session.execute(select(ExternalTradeRow).order_by(ExternalTradeRow.id))
        ).scalar_one()
    assert trade.status == "closed"
    assert trade.qty_opened == pytest.approx(2.0)
    assert trade.qty_closed == pytest.approx(2.0)
    assert trade.exit_price == pytest.approx(95.0)
    assert trade.realized_pnl == pytest.approx(-10.0)
    assert trade.source == "binance_external"
    assert await _count(store, ExchangeFillRow) == 3
    assert await _count(store, ExternalTradeFillRow) == 3
    assert await _count(store, TradeRow) == 0
    assert await _count(store, OrderRow) == 0


async def test_external_reversal_splits_fill_between_two_lifecycles(store):
    await store.ingest_exchange_fill(
        _fill("20", side="buy", qty=1.0, price=100.0, ts_ms=20_000)
    )
    await store.ingest_exchange_fill(
        _fill("21", side="sell", qty=1.5, price=105.0, ts_ms=21_000, realized_pnl=5.0)
    )
    async with store._sessionmaker() as session:
        trades = (
            await session.execute(select(ExternalTradeRow).order_by(ExternalTradeRow.id))
        ).scalars().all()
    assert len(trades) == 2
    assert trades[0].direction == "long"
    assert trades[0].status == "closed"
    assert trades[1].direction == "short"
    assert trades[1].status == "open"
    assert trades[1].qty_opened == pytest.approx(0.5)


async def test_external_fill_before_sync_window_is_archived_as_carry_in(store):
    await store.ingest_exchange_fill({
        **_fill("25", side="sell", qty=0.4, price=105.0, ts_ms=25_000, realized_pnl=2.0),
        "reduce_only": True,
    })
    async with store._sessionmaker() as session:
        trade = (
            await session.execute(select(ExternalTradeRow).order_by(ExternalTradeRow.id))
        ).scalar_one()
    assert trade.status == "closed"
    assert trade.direction == "long"
    assert trade.entry_price == 0
    assert trade.confidence == "carry_in"
    assert trade.realized_pnl == pytest.approx(2.0)
    assert trade.pnl_pct_on_margin == 0


async def test_unknown_fill_is_reclassified_after_position_claim_finishes(store):
    claim_id = await store.begin_position_claim(
        symbol="BTCUSDT", side="long", planned_qty=1.0, ttl_ms=60_000
    )
    fill = _fill(
        "26",
        side="buy",
        qty=0.2,
        price=100.0,
        ts_ms=int(time.time() * 1000),
        client_order_id="manual-during-claim",
    )
    result = await store.ingest_exchange_fill(fill)
    assert result["ownership"] == "unknown"
    assert await _count(store, ExternalTradeRow) == 0

    await store.finish_position_claim(claim_id, status="canceled")
    assert await store.resolve_unknown_exchange_fills() == 1
    async with store._sessionmaker() as session:
        ledger = (
            await session.execute(
                select(ExchangeFillRow).where(ExchangeFillRow.exchange_trade_id == "26")
            )
        ).scalar_one()
    assert ledger.ownership == "external"
    assert await _count(store, ExternalTradeRow) == 1


async def test_external_fill_during_strategy_trade_is_mixed_only(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "market", "qty": 1.0, "price": 100.0,
        "notional": 100.0, "status": "filled", "id": "engine-open",
        "client_order_id": "bt-engine-open", "raw": {},
    })
    result = await store.ingest_exchange_fill(
        _fill("30", side="sell", qty=0.25, price=101.0, ts_ms=int(time.time() * 1000))
    )
    assert result["ownership"] == "mixed"
    assert await _count(store, ExternalTradeRow) == 0
    assert await _count(store, TradeRow) == 1


async def test_connect_upgrades_position_projection_columns(tmp_path):
    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE live_positions (
            symbol VARCHAR(20) PRIMARY KEY,
            side VARCHAR(8),
            contracts FLOAT,
            entry_price FLOAT,
            mark_price FLOAT,
            leverage INTEGER,
            unrealized_pnl FLOAT,
            notional FLOAT,
            source VARCHAR(16),
            updated_at_ms INTEGER,
            raw_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER,
            created_at VARCHAR(32),
            symbol VARCHAR(20),
            side VARCHAR(8),
            contracts FLOAT,
            entry_price FLOAT,
            mark_price FLOAT,
            leverage INTEGER,
            unrealized_pnl FLOAT,
            notional FLOAT
        )
        """
    )
    con.commit()
    con.close()

    s = Store(str(db_path))
    await s.connect()
    async with s._engine.connect() as conn:
        live_cols = {
            row[1] for row in (await conn.execute(text("PRAGMA table_info(live_positions)"))).fetchall()
        }
        snapshot_cols = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(position_snapshots)"))).fetchall()
        }
    await s.close()

    for column in ("initial_margin", "isolated_margin", "roi_pct", "liquidation_price"):
        assert column in live_cols
        assert column in snapshot_cols


async def test_log_actual_decision_with_context(store):
    d = TradeDecision(symbol="BTCUSDT", action=Action.OPEN_LONG, confidence=0.8,
                      size_pct=0.1, leverage=3, stop_loss_pct=0.02,
                      take_profit_pct=0.04, reason="trend up")
    await store.log_decision(
        symbol="BTCUSDT",
        decision=d,
        ref_price=100.0,
        llm_system_prompt="system prompt",
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
    assert row.llm_system_prompt == "system prompt"
    assert row.llm_prompt == "prompt"
    assert "request" in row.llm_request_json
    assert "response" in row.llm_response_json
    assert "last_price" in row.feature_snapshot_json


async def test_llm_prompt_versions(store):
    assert await store.get_active_llm_prompt_version() is None
    first = await store.create_llm_prompt_version(
        name="trend",
        content="偏趋势交易",
        source="test",
        activate=True,
    )
    assert first["version"] == 1
    assert first["is_active"] is True
    assert first["content"] == "偏趋势交易"
    assert await store.get_llm_prompt_version(first["id"]) == first
    assert await store.get_llm_prompt_version(999999) is None
    second = await store.create_llm_prompt_version(
        name="flat",
        content="震荡少交易",
        source="test",
        activate=True,
    )
    active = await store.get_active_llm_prompt_version()
    assert active["id"] == second["id"]
    assert active["version"] == 2
    versions = await store.list_llm_prompt_versions()
    assert [v["version"] for v in versions[:2]] == [2, 1]
    await store.activate_llm_prompt_version(first["id"])
    active = await store.get_active_llm_prompt_version()
    assert active["id"] == first["id"]
    assert active["version"] == 1
    assert await _count(store, LLMPromptVersionRow) == 2
    full = await store.create_llm_prompt_version(
        name="full",
        content="",
        render_mode="full_template",
        system_prompt_template="system {x}",
        user_prompt_template="user {symbol}",
        notes="完整模板测试",
        source="test",
        activate=True,
    )
    assert full["version"] == 3
    assert full["render_mode"] == "full_template"
    assert full["system_prompt_template"] == "system {x}"
    assert full["user_prompt_template"] == "user {symbol}"
    assert full["notes"] == "完整模板测试"


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
        "initialMargin": 10.0, "collateral": 10.01, "liquidationPrice": 70.0,
        "marginRatio": 0.02, "marginMode": "isolated",
    }]
    await store.snapshot_positions(positions)
    assert await _count(store, PositionSnapshotRow) == 1
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        row = (await session.execute(select(PositionSnapshotRow))).scalars().one()
    assert row.initial_margin == pytest.approx(10.0)
    assert row.roi_pct == pytest.approx(0.1)
    assert row.liquidation_price == pytest.approx(70.0)

    rt = RuntimeState()
    rt.day_realized_pnl = -5.0
    await store.snapshot_balance(total_equity=200.0, available_margin=180.0, runtime=rt)
    assert await _count(store, BalanceSnapshotRow) == 1


async def test_live_position_state_includes_margin_roi_and_liquidation(store):
    await store.upsert_live_position({
        "symbol": "SOL/USDT:USDT",
        "side": "short",
        "contracts": 1.64,
        "entryPrice": 74.27,
        "markPrice": 74.38,
        "unrealizedPnl": -0.1804,
        "notional": 121.9832,
        "initialMargin": 24.39664,
        "collateral": 24.15579944,
        "liquidationPrice": 88.66581692,
        "marginRatio": 0.0252,
        "marginMode": "isolated",
    }, "rest")

    state = await store.live_account_state()

    assert await _count(store, LivePositionRow) == 1
    pos = state["positions"][0]
    assert pos["symbol"] == "SOLUSDT"
    assert pos["initial_margin"] == pytest.approx(24.39664)
    assert pos["isolated_margin"] == pytest.approx(24.15579944)
    assert pos["roi_pct"] == pytest.approx(-0.1804 / 24.39664 * 100)
    assert pos["liquidation_price"] == pytest.approx(88.66581692)
    assert pos["margin_mode"] == "isolated"


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


async def test_reconcile_symbol_flat_respects_time_guards(store):
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "market", "qty": 0.1, "price": 100.0,
        "notional": 10.0, "dry_run": False, "status": "filled",
        "id": "fresh-open", "raw": {},
    })

    assert await store.reconcile_symbol_flat("BTCUSDT", opened_before_ms=1) == 0
    assert await store.reconcile_symbol_flat("BTCUSDT", min_open_age_ms=60_000) == 0

    old_ms = int(time.time() * 1000) - 120_000
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
        trade.opened_at_ms = old_ms
        await session.commit()

    changed = await store.reconcile_symbol_flat(
        "BTCUSDT",
        opened_before_ms=old_ms + 1,
        min_open_age_ms=60_000,
    )

    assert changed == 1
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
    assert trade.status == "closed"
    assert trade.exit_reason == "EXCHANGE_FLAT"


async def test_reconcile_symbol_flat_uses_exchange_trades_provider_when_no_local_exit_order(store):
    """EXCHANGE_FLAT 路径：本地无 close 订单时，注入 provider 应回填真实平仓均价。"""
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "market", "qty": 0.0362, "price": 62810.65,
        "notional": 2273.0, "leverage": 10, "dry_run": False, "status": "filled",
        "id": "open-long", "raw": {},
    })
    # 把 opened_at_ms 调成 120s 之前，确保 min_open_age 不卡住
    old_ms = int(time.time() * 1000) - 120_000
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
        trade.opened_at_ms = old_ms
        await session.commit()

    # 模拟 ccxt myTrades：14:34 一次 SELL 全平 @ 62846.0
    open_ms = old_ms
    close_ms = open_ms + 60_000
    fetched_trades = [
        {
            "timestamp": close_ms,
            "side": "sell",
            "price": 62846.0,
            "amount": 0.0362,
            "fee": {"cost": 0.91, "currency": "USDT"},
            "info": {"side": "SELL"},
        }
    ]

    async def provider(symbol: str, since_ms: int, until_ms: int) -> list[dict]:
        assert symbol == "BTCUSDT"
        assert since_ms == open_ms
        assert until_ms >= close_ms
        return fetched_trades

    changed = await store.reconcile_symbol_flat(
        "BTCUSDT",
        reason="EXCHANGE_FLAT",
        min_open_age_ms=60_000,
        exchange_trades_provider=provider,
    )

    assert changed == 1
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
    assert trade.status == "closed"
    assert trade.exit_reason == "EXCHANGE_FLAT"
    assert trade.confidence == "inferred"
    # 退出均价来自交易所 myTrades，不再兜底为 entry_price
    assert trade.exit_price == pytest.approx(62846.0)
    # pnl = (62846.0 - 62810.65) * 0.0362 ≈ +1.28
    assert trade.realized_pnl == pytest.approx(1.28000, rel=1e-3)
    # 平仓手续费从 myTrades 累加进 total_fee
    assert trade.exit_fee == pytest.approx(0.91, rel=1e-6)
    # net 仍可能为负（pnl 抵不过 fee），但 gross 应该是 +1.28
    assert trade.gross_realized_pnl == pytest.approx(trade.realized_pnl, rel=1e-6)


async def test_reconcile_symbol_flat_falls_back_when_provider_fails(store):
    """EXCHANGE_FLAT 路径：provider 抛错时安全退回 entry_price 兜底，不阻断 reconcile。"""
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "market", "qty": 0.0362, "price": 62810.65,
        "notional": 2273.0, "leverage": 10, "dry_run": False, "status": "filled",
        "id": "open-long-2", "raw": {},
    })
    old_ms = int(time.time() * 1000) - 120_000
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
        trade.opened_at_ms = old_ms
        await session.commit()

    async def boom(symbol: str, since_ms: int, until_ms: int) -> list[dict]:
        raise RuntimeError("exchange timeout")

    changed = await store.reconcile_symbol_flat(
        "BTCUSDT",
        reason="EXCHANGE_FLAT",
        min_open_age_ms=60_000,
        exchange_trades_provider=boom,
    )

    assert changed == 1
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
    assert trade.status == "closed"
    assert trade.exit_reason == "EXCHANGE_FLAT"
    assert trade.confidence == "inferred"
    # provider 失败 → 退回 entry_price → pnl = 0
    assert trade.exit_price == pytest.approx(62810.65)
    assert trade.realized_pnl == pytest.approx(0.0, abs=1e-9)


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
    assert await store.has_recent_entry_claim("BTCUSDT") is True

    await store.finish_position_claim(
        claim_id,
        status="protecting",
        filled_qty=0.01,
        entry_price=100.0,
        client_order_id="open-1",
    )

    assert await store.has_active_position_claim("BTCUSDT") is True
    assert await store.has_recent_entry_claim("BTCUSDT") is True

    await store.finish_position_claim(
        claim_id,
        status="partial",
        filled_qty=0.01,
        entry_price=100.0,
        client_order_id="open-1",
    )

    assert await store.has_active_position_claim("BTCUSDT") is False
    assert await store.has_recent_entry_claim("BTCUSDT") is True
    assert await _count(store, PositionClaimRow) == 1


async def test_recent_entry_claim_ignores_expired_or_unfilled_terminal_claims(store):
    filled_claim = await store.begin_position_claim(
        symbol="BTCUSDT",
        side="long",
        planned_qty=0.1,
        ttl_ms=300_000,
    )
    await store.finish_position_claim(
        filled_claim,
        status="filled",
        filled_qty=0.1,
        entry_price=100.0,
    )
    empty_claim = await store.begin_position_claim(
        symbol="ETHUSDT",
        side="short",
        planned_qty=1.0,
        ttl_ms=300_000,
    )
    await store.finish_position_claim(empty_claim, status="rejected", filled_qty=0.0)

    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        row = await session.get(PositionClaimRow, filled_claim)
        row.expires_at_ms = 1
        await session.commit()

    assert await store.has_recent_entry_claim("BTCUSDT") is False
    assert await store.has_recent_entry_claim("ETHUSDT") is False


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

    filled_ts_ms = 1_781_594_103_083
    await store.mark_condition_exit(
        symbol="BTCUSDT",
        triggered_kind="TP",
        qty=1.0,
        price=109.5,
        ts_ms=filled_ts_ms,
    )

    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
        rows = (await session.execute(select(OrderRow).order_by(OrderRow.id))).scalars().all()
    by_kind = {row.client_kind: row for row in rows}

    assert trade.status == "closed"
    assert trade.exit_reason == "TP"
    assert trade.exit_price == pytest.approx(109.5)
    assert trade.closed_at_ms == filled_ts_ms
    assert trade.closed_at == "2026-06-16 07:15:03"
    assert trade.realized_pnl == pytest.approx(9.5)
    assert by_kind["TP"].realized_pnl == pytest.approx(9.5)
    assert by_kind["TP"].ts_ms == filled_ts_ms
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
    await store.set_symbol_enabled(
        "BTCUSDT",
        False,
        reason_code="PROTECTION_FAILURE",
        reason="SL missing",
        source="engine",
        action="disable_new_entries",
    )

    row = await store.get_symbol("BTCUSDT")
    assert row["enabled"] is False
    assert row["disabled_reason_code"] == "PROTECTION_FAILURE"
    assert row["disabled_reason"] == "SL missing"
    assert row["disabled_source"] == "engine"
    assert row["disabled_action"] == "disable_new_entries"
    assert row["disabled_at"]
    assert await store.get_runtime_setting("symbol.enabled.BTCUSDT") == "false"

    await store.set_symbol_enabled("BTCUSDT", True)
    row = await store.get_symbol("BTCUSDT")
    assert row["enabled"] is True
    assert row["disabled_reason_code"] == ""
    assert row["disabled_reason"] == ""
    assert row["last_enabled_at"]


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
    await store.mark_condition_exit(
        symbol="BTCUSDT",
        triggered_kind="TP",
        qty=0.01,
        price=94.5,
        ts_ms=1_781_594_103_083,
    )
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        rows = (await session.execute(select(OrderRow).order_by(OrderRow.id))).scalars().all()
    by_kind = {r.client_kind: r for r in rows}
    assert by_kind["TP"].status == "filled"
    assert by_kind["TP"].price == pytest.approx(95.0)
    raw = json.loads(by_kind["TP"].raw_json)
    assert raw["trigger_price"] == pytest.approx(95.0)
    assert raw["filled_price"] == pytest.approx(94.5)
    assert raw["filled_at_ms"] == 1_781_594_103_083
    assert by_kind["SL"].status == "canceled"


async def test_mark_condition_exit_locates_triggered_algo_order(store):
    opened = await store.log_order({
        "symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
        "order_type": "market", "qty": 0.01, "price": 100.0,
        "notional": 1.0, "dry_run": False, "status": "filled",
        "id": "open", "raw": {}, "leverage": 2,
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "SL", "side": "sell",
        "order_type": "STOP_MARKET", "qty": 0.01, "price": 101.0,
        "notional": 1.01, "dry_run": False, "status": "placed",
        "id": "2000001132311409", "client_order_id": "bt-sl",
        "raw": {}, "trade_id": opened["trade_id"],
    })
    await store.log_order({
        "symbol": "BTCUSDT", "kind": "TP", "side": "sell",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 0.01, "price": 110.0,
        "notional": 1.1, "dry_run": False, "status": "placed",
        "id": "2000001132311412", "client_order_id": "bt-tp",
        "raw": {}, "trade_id": opened["trade_id"],
    })

    await store.mark_condition_exit(
        symbol="BTCUSDT",
        qty=0.01,
        price=101.5,
        ts_ms=1_781_594_103_083,
        exchange_order_id="2000001132311409",
        client_order_id="bt-sl",
        fee=0.0001,
        fee_asset="USDT",
    )

    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        trade = (await session.execute(select(TradeRow))).scalar_one()
        rows = (await session.execute(select(OrderRow).order_by(OrderRow.id))).scalars().all()
    by_kind = {r.client_kind: r for r in rows}

    assert trade.status == "closed"
    assert trade.exit_reason == "SL"
    assert trade.closed_at_ms == 1_781_594_103_083
    assert by_kind["SL"].status == "filled"
    assert by_kind["SL"].avg_price == pytest.approx(101.5)
    assert by_kind["SL"].fee == pytest.approx(0.0001)
    assert by_kind["TP"].status == "canceled"


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


async def test_log_decision_persists_latency_fields(store):
    d = TradeDecision(symbol="ETHUSDT", action=Action.OPEN_LONG, confidence=0.8,
                      size_pct=0.1, leverage=3, stop_loss_pct=0.02,
                      take_profit_pct=0.04, reason="trend up")
    await store.log_decision(
        symbol="ETHUSDT",
        decision=d,
        ref_price=100.0,
        llm_latency_ms=4231,
        llm_attempts=1,
        llm_status="ok",
        llm_error="",
    )
    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        row = (await session.execute(select(DecisionRow))).scalar_one()
    assert row.llm_latency_ms == 4231
    assert row.llm_attempts == 1
    assert row.llm_status == "ok"
    assert row.llm_error == ""


async def test_decision_reason_uses_expanded_limit(store):
    assert DecisionRow.__table__.c.reason.type.length == DECISION_REASON_MAX_LENGTH

    decision_reason = "x" * DECISION_REASON_MAX_LENGTH
    d = TradeDecision(symbol="ETHUSDT", action=Action.OPEN_LONG, confidence=0.8,
                      size_pct=0.1, leverage=3, stop_loss_pct=0.02,
                      take_profit_pct=0.04, reason=decision_reason)
    await store.log_decision(symbol="ETHUSDT", decision=d, ref_price=100.0)
    await store.log_audit(
        symbol="ETHUSDT",
        action="PROFILE",
        reason="y" * (DECISION_REASON_MAX_LENGTH + 50),
    )

    sm = async_sessionmaker(store._engine, expire_on_commit=False)
    async with sm() as session:
        rows = (
            await session.execute(select(DecisionRow).order_by(DecisionRow.id))
        ).scalars().all()
    assert rows[0].reason == decision_reason
    assert len(rows[1].reason) == DECISION_REASON_MAX_LENGTH


import datetime as _dt


async def _seed_trade(store, *, day_dt, symbol, direction, net_pnl, status="closed"):
    from src.store.models import TradeRow
    ts_ms = int(day_dt.timestamp() * 1000)
    async with store._sessionmaker() as session:
        session.add(TradeRow(
            ts_ms=ts_ms, opened_at_ms=ts_ms,
            created_at=day_dt.strftime("%Y-%m-%d %H:%M:%S"),
            opened_at=day_dt.strftime("%Y-%m-%d %H:%M:%S"),
            closed_at_ms=ts_ms if status == "closed" else 0,
            closed_at=day_dt.strftime("%Y-%m-%d %H:%M:%S") if status == "closed" else "",
            symbol=symbol, direction=direction, status=status, dry_run=False,
            qty_opened=0.1, qty_closed=0.1 if status == "closed" else 0.0,
            entry_price=100.0, exit_price=100.0,
            net_realized_pnl=net_pnl,
        ))
        await session.commit()


async def test_day_realized_pnl_by_local_day_aggregates_close_only(store):
    today = _dt.datetime.now()
    yesterday = today - _dt.timedelta(days=1)
    day_before = today - _dt.timedelta(days=2)
    await _seed_trade(store, day_dt=today, symbol="BTCUSDT", direction="long", net_pnl=-1.5)
    await _seed_trade(store, day_dt=yesterday, symbol="BTCUSDT", direction="long", net_pnl=0.5)
    await _seed_trade(store, day_dt=day_before, symbol="ETHUSDT", direction="short", net_pnl=-2.0)
    # open trade 应被忽略
    await _seed_trade(store, day_dt=today, symbol="SOLUSDT", direction="long",
                      net_pnl=99.0, status="open")
    out = await store.day_realized_pnl_by_local_day()
    assert out[today.strftime("%Y-%m-%d")] == pytest.approx(-1.5)
    assert out[yesterday.strftime("%Y-%m-%d")] == pytest.approx(0.5)
    assert out[day_before.strftime("%Y-%m-%d")] == pytest.approx(-2.0)


async def test_day_realized_pnl_by_local_day_empty_when_no_trades(store):
    out = await store.day_realized_pnl_by_local_day()
    assert out == {}
