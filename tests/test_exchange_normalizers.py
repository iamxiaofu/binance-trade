"""exchange normalization helpers."""
from __future__ import annotations

import pytest

from src.exchange.orders import normalize_condition_order
from src.exchange.positions import normalize_position


def test_normalize_position_margin_roi_and_liquidation():
    pos = normalize_position({
        "symbol": "BTC/USDT:USDT",
        "side": "short",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "markPrice": 105.0,
        "unrealizedPnl": -0.5,
        "initialMargin": 10.0,
        "collateral": 9.5,
        "liquidationPrice": 150.0,
        "percentage": -5.0,
    })
    assert pos["symbol"] == "BTCUSDT"
    assert pos["initial_margin"] == pytest.approx(10.0)
    assert pos["isolated_margin"] == pytest.approx(9.5)
    assert pos["roi_pct"] == pytest.approx(-5.0)
    assert pos["liquidation_price"] == pytest.approx(150.0)


def test_normalize_condition_order_status_and_kind():
    order = normalize_condition_order({
        "id": "1",
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "amount": 0.1,
        "triggerPrice": 90.0,
        "status": "open",
        "info": {
            "orderType": "TAKE_PROFIT_MARKET",
            "algoStatus": "NEW",
            "updateTime": "1000",
        },
    })
    assert order["symbol"] == "BTCUSDT"
    assert order["kind"] == "TP"
    assert order["status"] == "placed"
    assert order["trigger_price"] == pytest.approx(90.0)
