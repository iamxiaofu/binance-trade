"""Web current-position protection display tests."""
from __future__ import annotations

from web.server import _attach_protection_orders


def test_attach_protection_ignores_historical_condition_orders():
    positions = [{"symbol": "BTCUSDT"}]
    orders = [
        {
            "symbol": "BTCUSDT",
            "kind": "SL",
            "status": "canceled",
            "trigger_price": 62278.2,
            "ts_ms": 20,
        },
        {
            "symbol": "BTCUSDT",
            "kind": "TP",
            "status": "rejected",
            "trigger_price": 60735.1,
            "ts_ms": 30,
        },
    ]

    _attach_protection_orders(positions, orders)

    protection = positions[0]["protection"]
    assert protection["sl"] is None
    assert protection["tp"] is None
    assert protection["missing_sl"] is True
    assert protection["missing_tp"] is True


def test_attach_protection_uses_active_placed_condition_orders():
    positions = [{"symbol": "BTCUSDT"}]
    orders = [
        {
            "symbol": "BTCUSDT",
            "kind": "SL",
            "status": "canceled",
            "trigger_price": 62278.2,
            "ts_ms": 20,
        },
        {
            "symbol": "BTCUSDT",
            "kind": "SL",
            "status": "placed",
            "trigger_price": 61000.0,
            "ts_ms": 10,
        },
    ]

    _attach_protection_orders(positions, orders)

    protection = positions[0]["protection"]
    assert protection["sl"]["status"] == "placed"
    assert protection["sl"]["trigger_price"] == 61000.0
    assert protection["missing_sl"] is False
    assert protection["missing_tp"] is True
