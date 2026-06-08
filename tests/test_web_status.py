"""web/status.py 测试：只读查询返回正确结构。"""
from __future__ import annotations

import sqlite3

import pytest

from src.llm.schema import IndicatorSnapshot, MarketContext, PositionSnapshot, TradeDecision
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
    ctx = MarketContext(
        symbol="ETHUSDT",
        timestamp=1,
        last_price=3000.0,
        mark_price=3001.0,
        funding_rate=0.0001,
        change_24h_pct=1.2,
        recent_klines=[[i, 1, 2, 0.5, 1.5, 100] for i in range(30)],
        micro_kline_interval="1m",
        micro_klines=[[i, 1, 2, 0.5, 1.5, 10] for i in range(30)],
        indicators=IndicatorSnapshot(
            ema_fast=1,
            ema_slow=2,
            rsi=55,
            macd=0.1,
            macd_signal=0.05,
            atr=10,
            boll_upper=4,
            boll_lower=1,
        ),
        position=PositionSnapshot(),
        available_margin=180.0,
        max_leverage_allowed=3,
        account_equity=200.0,
    )
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
        ctx=ctx,
        ref_price=3000.0,
        llm_prompt="stored prompt",
        llm_request_json='{"request": true}',
        llm_response_json='{"response": true}',
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


async def test_decision_detail_includes_llm_trace_and_data_items(db):
    detail = status.decision_detail(db, 3)
    assert detail is not None
    assert detail["llm_trace_available"] is True
    assert detail["llm_user_prompt"] == "stored prompt"
    assert "request" in detail["llm_request_effective_json"]
    fields = {item["field"] for item in detail["llm_data_items"]}
    assert {"last_price", "mark_price", "recent_klines_last20",
            "micro_klines_last30"}.issubset(fields)


async def test_balance_history_ascending(db):
    hist = status.balance_history(db)
    assert len(hist) == 1
    assert "total_equity" in hist[0]


async def test_balance_history_filters_time_range(db):
    s = Store(db)
    await s.connect()
    rt = RuntimeState()
    await s.snapshot_balance(total_equity=210.0, available_margin=190.0, runtime=rt)
    await s.snapshot_balance(total_equity=220.0, available_margin=200.0, runtime=rt)
    await s.close()

    conn = sqlite3.connect(db)
    try:
        ids = [row[0] for row in conn.execute("SELECT id FROM balance_snapshots ORDER BY id")]
        for ts, row_id in zip([1000, 2000, 3000], ids, strict=True):
            conn.execute("UPDATE balance_snapshots SET ts_ms = ? WHERE id = ?", (ts, row_id))
        conn.commit()
    finally:
        conn.close()

    hist = status.balance_history(db, start_ts_ms=1500, end_ts_ms=2500)
    assert [row["ts_ms"] for row in hist] == [2000]

    sampled = status.balance_history(db, limit=2, start_ts_ms=0, end_ts_ms=4000)
    assert len(sampled) == 2


async def test_pnl_stats(db):
    s = status.pnl_stats(db)
    assert s["close_count"] == 1
    assert s["range_close_count"] == 1
    assert s["trade_count"] == 1
    assert s["range_trade_count"] == 1
    assert s["close_by_symbol"] == {"BTCUSDT": 1}
    assert s["trade_by_symbol"] == {"BTCUSDT": 1}
    assert "range_net_realized_pnl" in s


def test_resolve_time_bounds_quick_range():
    start, end, key = status.resolve_time_bounds(range_key="3h", now_ms=10_800_000)
    assert key == "3h"
    assert start == 0
    assert end == 10_800_000


def test_resolve_time_bounds_custom_range():
    start, end, key = status.resolve_time_bounds(
        start_ts_ms=1000,
        end_ts_ms=2000,
        range_key="1h",
    )
    assert key == "custom"
    assert start == 1000
    assert end == 2000


def test_pnl_stats_filters_time_range(db):
    rows = status.recent_orders(db)
    close = next(row for row in rows if row["client_kind"] == "CLOSE")
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE orders SET ts_ms = 1000 WHERE client_kind = 'OPEN'")
        conn.execute("UPDATE orders SET ts_ms = 5000 WHERE client_kind = 'CLOSE'")
        conn.execute("UPDATE trades SET opened_at_ms = 1000")
        conn.commit()
    finally:
        conn.close()

    empty = status.pnl_stats(db, status.PnlFilters(start_ts_ms=0, end_ts_ms=2000))
    assert empty["range_close_count"] == 0
    assert empty["range_trade_count"] == 1

    matched = status.pnl_stats(db, status.PnlFilters(start_ts_ms=4000, end_ts_ms=6000))
    assert matched["range_close_count"] == 1
    assert matched["range_trade_count"] == 0
    assert matched["close_by_symbol"] == {close["symbol"]: 1}


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
