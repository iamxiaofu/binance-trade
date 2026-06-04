"""合约精度处理测试（SPEC 要求覆盖 tickSize/stepSize/minNotional）。"""
from __future__ import annotations

from decimal import Decimal

from src.exchange.filters import (
    SymbolFilters,
    normalize_order,
    round_price,
    round_qty,
)

F = SymbolFilters(
    tick_size=Decimal("0.1"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("5"),
)


def test_round_qty_floors_to_step():
    assert round_qty(0.0123456, F) == Decimal("0.012")


def test_round_price_to_tick():
    assert round_price(65000.07, F) == Decimal("65000.1")
    assert round_price(65000.04, F) == Decimal("65000.0")


def test_normalize_market_order():
    o = normalize_order(qty=0.0123456, price=65000.07, f=F, is_market=True)
    assert o is not None
    assert o.qty == Decimal("0.012")
    assert o.price is None  # market
    assert o.notional == Decimal("0.012") * Decimal("65000.1")


def test_normalize_limit_order_keeps_price():
    o = normalize_order(qty=0.01, price=65000.07, f=F, is_market=False)
    assert o.price == Decimal("65000.1")


def test_below_min_notional_returns_none():
    assert normalize_order(qty=0.00005, price=65000, f=F, is_market=True) is None


def test_below_min_qty_returns_none():
    tiny = SymbolFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("1"),
    )
    # 0.005 floors to 0.005 < min_qty 0.01
    assert normalize_order(qty=0.005, price=65000, f=tiny, is_market=True) is None


def test_from_ccxt_precision_as_digits():
    # 有些交易所 precision 用「小数位数」表示：2 → 0.01
    mkt = {"precision": {"price": 2, "amount": 3}, "limits": {}}
    f = SymbolFilters.from_ccxt_market(mkt)
    assert f.tick_size == Decimal("0.01")
    assert f.step_size == Decimal("0.001")


def test_from_ccxt_precision_as_step():
    mkt = {
        "precision": {"price": 0.1, "amount": 0.001},
        "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}},
    }
    f = SymbolFilters.from_ccxt_market(mkt)
    assert f.tick_size == Decimal("0.1")
    assert f.min_notional == Decimal("5")
