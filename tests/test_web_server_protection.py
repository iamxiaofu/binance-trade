"""Web current-position protection display tests."""
from __future__ import annotations

import pytest

from web import server
from web.server import _apply_live_balance, _attach_projection_metadata


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

    _attach_projection_metadata(positions, orders)

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

    _attach_projection_metadata(positions, orders)

    protection = positions[0]["protection"]
    assert protection["sl"]["status"] == "placed"
    assert protection["sl"]["trigger_price"] == 61000.0
    assert protection["missing_sl"] is False
    assert protection["missing_tp"] is True


def test_attach_protection_exposes_multiple_tp_orders_and_coverage():
    positions = [{"symbol": "SOLUSDT", "contracts": 4.51}]
    orders = [
        {
            "id": "sl",
            "symbol": "SOLUSDT",
            "kind": "SL",
            "status": "placed",
            "trigger_price": 69.88,
            "qty": 0.0,
            "close_position": True,
            "origin": "EXTERNAL",
        },
        {
            "id": "tp-2",
            "symbol": "SOLUSDT",
            "kind": "TP",
            "status": "placed",
            "trigger_price": 73.0,
            "qty": 2.25,
            "origin": "EXTERNAL",
        },
        {
            "id": "tp-1",
            "symbol": "SOLUSDT",
            "kind": "TP",
            "status": "placed",
            "trigger_price": 72.23,
            "qty": 2.25,
            "origin": "EXTERNAL",
        },
    ]

    _attach_projection_metadata(positions, orders)

    protection = positions[0]["protection"]
    assert [row["id"] for row in protection["tp_orders"]] == ["tp-1", "tp-2"]
    assert protection["tp_covered_qty"] == pytest.approx(4.5)
    assert protection["tp_coverage_pct"] == pytest.approx(4.5 / 4.51)
    assert protection["runner_qty"] == pytest.approx(0.01)
    assert protection["authority"] == "EXTERNAL"
    assert protection["mode"] == "OBSERVE"
    assert protection["status"] == "PARTIAL_TP_COVERAGE"


def test_attach_protection_reports_over_coverage_conflict():
    positions = [{"symbol": "SOLUSDT", "contracts": 4.0}]
    orders = [
        {
            "id": "sl",
            "symbol": "SOLUSDT",
            "kind": "SL",
            "status": "placed",
            "trigger_price": 69.0,
            "qty": 4.0,
            "origin": "ENGINE",
        },
        {
            "id": "tp-1",
            "symbol": "SOLUSDT",
            "kind": "TP",
            "status": "placed",
            "trigger_price": 73.0,
            "qty": 2.5,
            "origin": "ENGINE",
        },
        {
            "id": "tp-2",
            "symbol": "SOLUSDT",
            "kind": "TP",
            "status": "placed",
            "trigger_price": 74.0,
            "qty": 2.5,
            "origin": "ENGINE",
        },
    ]

    _attach_projection_metadata(positions, orders)

    protection = positions[0]["protection"]
    assert protection["status"] == "CONFLICT"
    assert protection["conflicts"] == ["TP_OVER_COVERED"]
    assert protection["tp_ordered_qty"] == pytest.approx(5.0)


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


def test_account_risk_metrics_separate_breaker_position_and_orders():
    summary = {
        "balance": {
            "total_equity": 200.0,
            "available_margin": 120.0,
            "drawdown_pct": 10.0,
        },
        "positions": [{
            "symbol": "BTCUSDT",
            "unrealized_pnl": -5.0,
            "initial_margin": 30.0,
        }],
        "open_orders": [
            {"origin": "EXTERNAL"},
            {"origin": "ENGINE"},
        ],
    }

    server._apply_account_risk_metrics(summary, equity_peak=222.222222)

    balance = summary["balance"]
    assert balance["account_drawdown_pct"] == pytest.approx(10.0)
    assert balance["account_equity_peak"] == pytest.approx(222.222222)
    assert balance["position_unrealized_pnl"] == pytest.approx(-5.0)
    assert balance["position_floating_loss"] == pytest.approx(5.0)
    assert balance["position_floating_loss_pct_equity"] == pytest.approx(2.5)
    assert balance["unavailable_margin"] == pytest.approx(80.0)
    assert balance["open_order_reserved_margin_estimate"] == pytest.approx(50.0)
    assert balance["regular_open_order_count"] == 2
    assert balance["external_open_order_count"] == 1
    assert balance["drawdown_breaker_basis"] == "ACCOUNT_EQUITY_HIGH_WATER_MARK"


async def test_status_summary_separates_regular_and_condition_orders(monkeypatch):
    class FakeStore:
        async def get_runtime_setting(self, key):
            return "250" if key == "risk.equity_peak" else None

        async def live_account_state(self):
            return {
                "balances": [{
                    "asset": server._settings.account.quote_asset,
                    "wallet_balance": 200.0,
                    "available_balance": 180.0,
                    "updated_at_ms": 1000,
                }],
                "positions": [{
                    "symbol": "BTCUSDT",
                    "updated_at_ms": 1000,
                    "unrealized_pnl": -2.0,
                    "initial_margin": 10.0,
                }],
                "open_orders": [
                    {
                        "symbol": "BTCUSDT",
                        "id": "limit-1",
                        "order_class": "regular",
                        "status": "open",
                        "updated_at_ms": 1000,
                    },
                    {
                        "symbol": "BTCUSDT",
                        "id": "sl-1",
                        "order_class": "algo",
                        "kind": "SL",
                        "status": "placed",
                        "updated_at_ms": 1000,
                    },
                ],
            }

    async def fake_get_store():
        return FakeStore()

    async def fake_effective_symbol_enabled():
        return {}

    monkeypatch.setattr(server.st, "status_summary", lambda _db: {"balance": {}, "positions": []})
    monkeypatch.setattr(server.st, "day_equity_change", lambda *_args, **_kw: {})
    monkeypatch.setattr(server, "_get_store", fake_get_store)
    monkeypatch.setattr(server, "_effective_symbol_enabled", fake_effective_symbol_enabled)

    summary = await server._status_summary()

    assert [row["id"] for row in summary["open_orders"]] == ["limit-1"]
    assert [row["id"] for row in summary["condition_orders"]] == ["sl-1"]
    assert [row["id"] for row in summary["all_open_orders"]] == ["limit-1", "sl-1"]
    assert summary["balance"]["account_equity_peak"] == pytest.approx(250.0)
    assert summary["balance"]["position_floating_loss"] == pytest.approx(2.0)
    assert summary["balance"]["regular_open_order_count"] == 1


def test_projection_metadata_applies_local_trade_fields(monkeypatch):
    monkeypatch.setattr(
        server.st,
        "open_trade_metadata",
        lambda _db: {"BTCUSDT": {"trade_id": 7, "local_leverage": 5}},
    )
    positions = [{"symbol": "BTCUSDT", "leverage": 0}]
    _attach_projection_metadata(positions, [])
    assert positions[0]["trade_id"] == 7
    assert positions[0]["leverage"] == 5
