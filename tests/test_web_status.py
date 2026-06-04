"""web/status.py 测试：只读查询返回正确结构。"""
from __future__ import annotations

import pytest

from src.state.runtime import RuntimeState
from src.store.repo import Store
from web import status


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "t.db")
    s = Store(path)
    await s.connect()
    await s.log_decision(symbol="BTCUSDT", skipped=True, skip_reason="flat", ref_price=100.0)
    await s.log_order({"symbol": "BTCUSDT", "kind": "OPEN", "side": "buy",
                       "order_type": "market", "qty": 0.01, "price": 100.0,
                       "notional": 1.0, "dry_run": True, "status": "dry_run", "id": "", "raw": {}})
    await s.snapshot_positions([{"symbol": "BTC/USDT:USDT", "side": "long",
                                 "contracts": 0.01, "entryPrice": 100.0, "markPrice": 101.0,
                                 "leverage": 3, "unrealizedPnl": 0.01}])
    rt = RuntimeState()
    await s.snapshot_balance(total_equity=200.0, available_margin=180.0, runtime=rt)
    await s.close()
    return path


async def test_recent_queries(db):
    assert len(status.recent_decisions(db)) == 1
    assert len(status.recent_orders(db)) == 1
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
    # 预置只有一条 OPEN 订单，无平仓 → close_count=0
    assert s["close_count"] == 0


async def test_decision_detail(db):
    d0 = status.recent_decisions(db)[0]
    detail = status.decision_detail(db, d0["id"])
    assert detail is not None and detail["id"] == d0["id"]
    assert status.decision_detail(db, 999999) is None


async def test_missing_table_degrades_to_empty(tmp_path):
    # 全新空库（无表）→ 查询不应抛错，返回空
    empty = str(tmp_path / "empty.db")
    import sqlite3
    sqlite3.connect(empty).close()  # 建立空文件
    assert status.recent_orders(empty) == []
    assert status.latest_balance(empty) is None
