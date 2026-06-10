"""``src.exchange.orders.normalize_open_order`` 单测。"""
from __future__ import annotations

from src.exchange.orders import normalize_open_order


def test_normalize_open_order_extracts_ccxt_fields():
    raw = {
        "id": "OID-1",
        "clientOrderId": "bt-x",
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "type": "limit",
        "amount": 0.05,
        "price": 63000.5,
        "filled": 0.01,
        "average": 63001.0,
        "status": "open",
        "timeInForce": "GTC",
        "reduceOnly": True,
        "info": {
            "orderId": "OID-1",
            "side": "BUY",
            "timeInForce": "GTC",
            "updateTime": 1700000000000,
        },
    }
    out = normalize_open_order(raw)
    assert out["id"] == "OID-1"
    assert out["client_order_id"] == "bt-x"
    assert out["symbol"] == "BTCUSDT"
    assert out["side"] == "buy"
    assert out["order_type"] == "LIMIT"
    assert out["qty"] == 0.05
    assert out["price"] == 63000.5
    assert out["filled_qty"] == 0.01
    assert out["avg_price"] == 63001.0
    assert out["status"] == "placed"
    assert out["raw_status"] == "open"
    assert out["time_in_force"] == "GTC"
    assert out["reduce_only"] is True
    assert out["ts_ms"] == 1700000000000


def test_normalize_open_order_handles_missing_amount():
    raw = {
        "id": "OID-2",
        "symbol": "ETH/USDT:USDT",
        "side": "sell",
        "price": 1700.0,
        "info": {"orderId": "OID-2", "side": "SELL", "status": "NEW", "type": "LIMIT"},
    }
    out = normalize_open_order(raw)
    assert out["symbol"] == "ETHUSDT"
    assert out["qty"] == 0.0
    assert out["price"] == 1700.0
    assert out["status"] == "placed"
