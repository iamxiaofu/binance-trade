"""web/status.py 测试：只读查询返回正确结构。"""
from __future__ import annotations

import pytest

from src.llm.schema import TradeDecision
from src.state.runtime import RuntimeState
from src.store.repo import Store
from web import status


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "t.db")
    s = Store(path)
    await s.connect()
    await s.log_decision(symbol="BTCUSDT", skipped=True, skip_reason="flat", ref_price=100.0)
    await s.log_decision(symbol="BNBUSDT", skipped=True, skip_reason="symbol disabled", ref_price=600.0)
    await s.log_decision(
        symbol="ETHUSDT",
        decision=TradeDecision(
            symbol="ETHUSDT",
            action="OPEN_LONG",
            confidence=0.7,
            size_pct=0.1,
            leverage=3,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            reason="trend",
        ),
        ref_price=3000.0,
    )
    await s.log_order({"symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
                       "order_type": "market", "qty": 0.01, "price": 100.0,
                       "notional": 1.0, "dry_run": False, "status": "filled",
                       "id": "open", "raw": {}, "leverage": 2})
    await s.log_order({"symbol": "BTCUSDT", "kind": "CLOSE", "side": "sell",
                       "order_type": "market", "qty": 0.01, "price": 110.0,
                       "notional": 1.1, "dry_run": False, "status": "filled", "id": "close", "raw": {}})
    await s.snapshot_positions([{"symbol": "BTC/USDT:USDT", "side": "long",
                                 "contracts": 0.01, "entryPrice": 100.0, "markPrice": 101.0,
                                 "leverage": 3, "unrealizedPnl": 0.01}])
    rt = RuntimeState()
    await s.snapshot_balance(total_equity=200.0, available_margin=180.0, runtime=rt)
    await s.close()
    return path


async def test_recent_queries(db):
    assert len(status.recent_decisions(db)) == 3
    assert len(status.recent_orders(db)) == 2
    assert status.recent_rejects(db) == []


async def test_latest_positions_and_balance(db):
    pos = status.latest_positions(db)
    assert len(pos) == 1 and pos[0]["symbol"] == "BTCUSDT"
    bal = status.latest_balance(db)
    assert bal is not None and bal["total_equity"] == 200.0


async def test_latest_positions_ignores_latest_zero_snapshot(db):
    s = Store(db)
    await s.connect()
    await s.snapshot_positions([], symbols=["BTCUSDT"])
    await s.close()
    assert status.latest_positions(db) == []


async def test_status_summary_shape(db):
    s = status.status_summary(db)
    assert set(s) == {"balance", "positions", "recent_decisions", "recent_orders",
                      "recent_rejects", "recent_commands"}


async def test_balance_history_ascending(db):
    hist = status.balance_history(db)
    assert len(hist) == 1
    assert "total_equity" in hist[0]


async def test_pnl_stats(db):
    s = status.pnl_stats(db)
    assert set(s) == {"day_realized_pnl", "close_count", "close_by_symbol"}
    assert s["close_count"] == 1
    assert s["close_by_symbol"] == {"BTCUSDT": 1}


async def test_decision_detail(db):
    d0 = status.recent_decisions(db)[0]
    detail = status.decision_detail(db, d0["id"])
    assert detail is not None and detail["id"] == d0["id"]
    assert status.decision_detail(db, 999999) is None


def test_search_decisions_filters_symbol(db):
    res = status.search_decisions(db, status.DecisionFilters(symbols=["ETHUSDT"]))
    assert res["total"] == 1
    assert res["items"][0]["symbol"] == "ETHUSDT"


def test_search_decisions_filters_type(db):
    res = status.search_decisions(db, status.DecisionFilters(types=["OPEN_LONG"]))
    assert res["total"] == 1
    assert res["items"][0]["action"] == "OPEN_LONG"

    skipped = status.search_decisions(db, status.DecisionFilters(types=["SKIPPED"]))
    assert skipped["total"] == 2
    assert all(row["skipped"] for row in skipped["items"])


def test_search_decisions_filters_time_range(db):
    rows = status.recent_decisions(db)
    target = next(row for row in rows if row["symbol"] == "ETHUSDT")
    res = status.search_decisions(
        db,
        status.DecisionFilters(
            start_ts_ms=target["ts_ms"],
            end_ts_ms=target["ts_ms"],
        ),
    )
    assert res["total"] >= 1
    assert all(row["ts_ms"] == target["ts_ms"] for row in res["items"])


def test_search_decisions_hides_symbol_disabled(db):
    res = status.search_decisions(db, status.DecisionFilters(hide_symbol_disabled=True))
    assert res["total"] == 2
    assert all(row["skip_reason"] != "symbol disabled" for row in res["items"])


def test_search_decisions_pagination(db):
    res = status.search_decisions(db, status.DecisionFilters(limit=1, offset=1))
    assert res["total"] == 3
    assert len(res["items"]) == 1


def test_search_trades_returns_grouped_orders_and_pnl(db):
    res = status.search_trades(db, status.TradeFilters(symbols=["BTCUSDT"]))
    assert res["total"] == 1
    trade = res["items"][0]
    assert trade["symbol"] == "BTCUSDT"
    assert trade["direction"] == "long"
    assert trade["status"] == "closed"
    assert trade["entry_margin"] == pytest.approx(0.5)
    assert trade["realized_pnl"] == pytest.approx(0.1)
    assert trade["pnl_pct_on_margin"] == pytest.approx(20.0)
    assert len(trade["orders"]) == 2


def test_search_trades_filters_direction_and_status(db):
    res = status.search_trades(
        db,
        status.TradeFilters(directions=["long"], statuses=["closed"], limit=10),
    )
    assert res["total"] == 1
    assert res["items"][0]["exit_reason"] == "CLOSE"

    empty = status.search_trades(db, status.TradeFilters(directions=["short"]))
    assert empty["total"] == 0


async def test_missing_table_degrades_to_empty(tmp_path):
    # 全新空库（无表）→ 查询不应抛错，返回空
    empty = str(tmp_path / "empty.db")
    import sqlite3
    sqlite3.connect(empty).close()  # 建立空文件
    assert status.recent_orders(empty) == []
    assert status.latest_balance(empty) is None
