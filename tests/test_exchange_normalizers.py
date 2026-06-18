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


def test_normalize_condition_order_reads_algo_update_tp_trigger_price():
    order = normalize_condition_order({
        "info": {
            "algoId": 2000001144654254,
            "clientAlgoId": "ios-example",
            "symbol": "SOLUSDT",
            "side": "SELL",
            "orderType": "TAKE_PROFIT_MARKET",
            "quantity": "4.51",
            "price": "0",
            "tp": "72.23",
            "algoStatus": "NEW",
            "reduceOnly": True,
        },
    })
    assert order["kind"] == "TP"
    assert order["status"] == "placed"
    assert order["price"] == 0
    assert order["trigger_price"] == pytest.approx(72.23)


def test_normalize_position_isolated_derives_leverage_when_null():
    """B6：ISOLATED 模式交易所不返回 leverage，从 notional/initial_margin 反推。"""
    pos = normalize_position({
        "symbol": "ETH/USDT:USDT",
        "side": "long",
        "contracts": 0.088,
        "entryPrice": 1696.75,
        "markPrice": 1665.99,
        "leverage": None,  # 交易所侧不返回
        "notional": 146.607,
        "initialMargin": 48.869,
        "isolatedMargin": 47.024,
        "info": {
            "positionAmt": "0.088",
            "notional": "146.607",
            "initialMargin": "48.869",
        },
    })
    # 146.607 / 48.869 ≈ 3.0 → leverage 3
    assert pos["leverage"] == 3


def test_normalize_position_explicit_leverage_takes_priority():
    """B6：交易所明确返回 leverage 时优先使用，不去反推。"""
    pos = normalize_position({
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 0.1,
        "entryPrice": 100.0,
        "leverage": 10,  # 交易所明确给出
        "notional": 100.0,
        "initialMargin": 50.0,
    })
    assert pos["leverage"] == 10


def test_normalize_position_no_margin_returns_zero_leverage():
    """B6：缺保证金数据时回退 0，不臆造。"""
    pos = normalize_position({
        "symbol": "X/USDT:USDT",
        "side": "long",
        "contracts": 0.0,
        "entryPrice": 0.0,
        "leverage": None,
        "notional": 0.0,
        "initialMargin": 0.0,
    })
    assert pos["leverage"] == 0
