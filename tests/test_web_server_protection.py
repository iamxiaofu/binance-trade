"""Web current-position protection display tests."""
from __future__ import annotations

import pytest

from web import server
from web.server import _apply_live_balance, _attach_protection_orders, _live_balance_snapshot


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


def test_apply_live_balance_recomputes_day_equity_from_exchange(monkeypatch):
    monkeypatch.setattr(server, "_DB", "/tmp/test.db")
    monkeypatch.setattr(
        server.st,
        "day_equity_change",
        lambda db_path, **kw: {
            "day_equity_change": kw["current_equity"] - 100.0,
            "day_equity_start": 100.0,
            "day_equity_latest": kw["current_equity"],
            "day_equity_start_ts_ms": 1000,
            "day_equity_start_snapshot_ts_ms": 900,
            "day_equity_start_snapshot_at": "before",
        },
    )
    summary = {"balance": {"total_equity": 101.0, "available_margin": 90.0}}

    _apply_live_balance(
        summary,
        {"ts_ms": 2000, "total_equity": 123.45, "available_margin": 120.0},
    )

    balance = summary["balance"]
    assert balance["ts_ms"] == 2000
    assert balance["total_equity"] == pytest.approx(123.45)
    assert balance["available_margin"] == pytest.approx(120.0)
    assert balance["day_equity_change"] == pytest.approx(23.45)
    assert balance["equity_source"] == "exchange"


async def test_live_balance_snapshot_uses_cache(monkeypatch):
    calls = 0

    class FakeClient:
        async def fetch_balance(self):
            nonlocal calls
            calls += 1
            return {"total": {"USDT": 150.0}, "free": {"USDT": 140.0}}

    async def get_market():
        return FakeClient()

    monkeypatch.setattr(server, "_get_market", get_market)
    server._balance_cache.update({"ts_ms": 0, "total_equity": None, "available_margin": None})

    first = await _live_balance_snapshot()
    second = await _live_balance_snapshot()

    assert calls == 1
    assert first["total_equity"] == pytest.approx(150.0)
    assert second["total_equity"] == pytest.approx(150.0)
