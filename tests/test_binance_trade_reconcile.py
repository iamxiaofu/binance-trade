from __future__ import annotations

from decimal import Decimal

import pytest

from src.reconcile.binance_trades import (
    CanonicalFill,
    replay_trade_cycles,
    validate_replay,
)
from src.reconcile.service import BinanceTradeReconciler


def _fill(
    fill_id: int,
    *,
    side: str,
    qty: str,
    price: str,
    ownership: str = "external",
    pnl: str = "0",
    fee: str = "0.1",
    reduce_only: bool = False,
    exit_reason: str = "",
) -> CanonicalFill:
    return CanonicalFill(
        exchange_fill_id=fill_id,
        ts_ms=fill_id * 1000,
        symbol="SOLUSDT",
        exchange_trade_id=str(fill_id),
        exchange_order_id=f"order-{fill_id}",
        client_order_id="bt-engine" if ownership == "engine" else "manual",
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        realized_pnl=Decimal(pnl),
        liquidity="taker",
        reduce_only=reduce_only,
        ownership=ownership,
        exit_reason=exit_reason,
    )


def test_partial_take_profit_and_manual_remainder_stay_one_cycle():
    fills = [
        _fill(1, side="sell", qty="8.43", price="72.41"),
        _fill(
            2, side="buy", qty="4.21", price="71.58", pnl="3.4943",
            reduce_only=True, exit_reason="TP",
        ),
        _fill(
            3, side="buy", qty="4.22", price="72.00", pnl="1.7302",
            reduce_only=True, exit_reason="MANUAL_REDUCE",
        ),
    ]

    result = replay_trade_cycles(fills)

    assert validate_replay(fills, result) == []
    assert len(result.cycles) == 1
    cycle = result.cycles[0]
    assert cycle.status == "closed"
    assert cycle.qty_opened == Decimal("8.43")
    assert cycle.qty_closed == Decimal("8.43")
    assert float(cycle.exit_price) == pytest.approx(71.7902491103)
    assert cycle.realized_pnl == Decimal("5.2245")
    assert cycle.exit_reason == "MIXED_EXIT"
    assert [row.role for row in cycle.allocations] == ["ENTRY", "EXIT", "EXIT"]


def test_partial_stop_loss_does_not_create_second_trade():
    fills = [
        _fill(10, side="buy", qty="2", price="100"),
        _fill(
            11, side="sell", qty="0.5", price="95", pnl="-2.5",
            reduce_only=True, exit_reason="SL",
        ),
    ]

    result = replay_trade_cycles(fills)

    assert validate_replay(fills, result) == []
    assert len(result.cycles) == 1
    assert result.cycles[0].status == "partial"
    assert result.cycles[0].open_qty == Decimal("1.5")
    assert result.cycles[0].exit_reason == "SL"


def test_reversal_assigns_all_realized_pnl_to_close_and_splits_fee_only():
    fills = [
        _fill(20, side="buy", qty="1", price="100", fee="0.1"),
        _fill(21, side="sell", qty="1.5", price="105", pnl="5", fee="0.3"),
    ]

    result = replay_trade_cycles(fills)

    assert validate_replay(fills, result) == []
    assert len(result.cycles) == 2
    closed, opened = result.cycles
    assert closed.realized_pnl == Decimal("5")
    assert closed.exit_fee == Decimal("0.2")
    assert opened.entry_fee == Decimal("0.1")
    assert opened.qty_opened == Decimal("0.5")


def test_external_entry_engine_exit_is_mixed_lifecycle_not_mixed_fill():
    fills = [
        _fill(30, side="buy", qty="1", price="100", ownership="external"),
        _fill(
            31, side="sell", qty="1", price="101", ownership="engine",
            pnl="1", reduce_only=True, exit_reason="CLOSE",
        ),
    ]

    result = replay_trade_cycles(fills)

    assert validate_replay(fills, result) == []
    assert result.cycles[0].ownership == "mixed"
    assert [row.fill_ownership for row in result.cycles[0].allocations] == [
        "external", "engine",
    ]


def test_reduce_only_cannot_reverse_position():
    fills = [
        _fill(40, side="buy", qty="1", price="100"),
        _fill(
            41, side="sell", qty="1.1", price="99", pnl="-1",
            reduce_only=True, exit_reason="SL",
        ),
    ]

    result = replay_trade_cycles(fills)

    assert any("reduce-only fill exceeds" in error for error in validate_replay(fills, result))


def test_order_metadata_resolves_triggered_algo_and_manual_reduce():
    local = [{
        "id": 1,
        "ts_ms": 1,
        "symbol": "SOLUSDT",
        "exchange_trade_id": "1",
        "exchange_order_id": "100",
        "client_order_id": "",
        "side": "buy",
        "qty": 1.0,
        "price": 70.0,
        "fee": 0.1,
        "fee_asset": "USDT",
        "realized_pnl": 1.0,
        "liquidity": "taker",
        "reduce_only": False,
        "ownership": "mixed",
    }]
    orders = {("SOLUSDT", "100"): {
        "clientOrderId": "bt-triggered",
        "reduceOnly": True,
        "type": "MARKET",
    }}
    events = {"100": {
        "algo_id": "200",
        "order_type": "TAKE_PROFIT_MARKET",
        "reduce_only": True,
        "client_order_id": "bt-triggered",
    }}

    resolved = BinanceTradeReconciler._resolve_fills(local, orders, events)[0]

    assert resolved["ownership"] == "engine"
    assert resolved["exit_reason"] == "TP"
    assert resolved["algo_id"] == "200"
