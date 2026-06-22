"""web/status.py 测试：只读查询返回正确结构。"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from src.llm.schema import Action, IndicatorSnapshot, MarketContext, PositionSnapshot, TradeDecision
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
        llm_system_prompt="stored system prompt",
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
                                 "leverage": 3, "unrealizedPnl": 0.01,
                                 "initialMargin": 10.0, "liquidationPrice": 70.0}])
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
    assert pos[0]["roi_pct"] == 0.1
    assert pos[0]["liquidation_price"] == 70.0
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


async def test_status_summary_limits_recent_decisions(db):
    s = Store(db)
    await s.connect()
    for i in range(6):
        await s.log_decision(symbol="BTCUSDT", skipped=True, skip_reason=f"extra-{i}", ref_price=100.0 + i)
    await s.close()

    summary = status.status_summary(db)

    assert len(summary["recent_decisions"]) == 5


async def test_trade_search_prefers_active_reconciled_generation(db):
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO exchange_reconcile_runs "
            "(id, created_at_ms, created_at, applied_at_ms, status, scope_start_ms, "
            "scope_end_ms, preview_hash, local_fill_count, remote_fill_count, "
            "cycle_count, ownership_change_count, metadata_change_count, summary_json, error) "
            "VALUES (1, 1, '', 1, 'applied', 0, 1, ?, 1, 1, 1, 0, 0, '{}', '')",
            ("a" * 64,),
        )
        con.execute(
            "INSERT INTO runtime_settings (key, value, updated_at) VALUES (?, ?, '')",
            ("binance.trade_cycles.active_run_id", "1"),
        )
        con.execute(
            """
            INSERT INTO binance_trade_cycles
            (id, run_id, sequence, symbol, direction, ownership, status,
             opened_at_ms, opened_at, closed_at_ms, closed_at,
             entry_price, exit_price, qty_opened, qty_closed,
             entry_notional, exit_notional, entry_fee, exit_fee, total_fee,
             gross_realized_pnl, net_realized_pnl, entry_liquidity, exit_liquidity,
             exit_reason, confidence, classification_reason)
            VALUES
            (10, 1, 1, 'SOLUSDT', 'short', 'mixed', 'closed',
             1000, '', 2000, '', 72, 71, 2, 2, 144, 142, 0.1, 0.1, 0.2,
             2, 1.8, 'taker', 'taker', 'TP', 'exact', 'mixed lifecycle')
            """
        )
        con.execute(
            """
            INSERT INTO exchange_fills
            (id, ts_ms, created_at, symbol, exchange_trade_id, exchange_order_id,
             client_order_id, side, qty, price, notional, fee, fee_asset,
             realized_pnl, liquidity, reduce_only, ownership,
             classification_reason, source, raw_json,
             resolved_ownership, resolved_client_order_id, resolved_reduce_only,
             resolved_order_type, resolved_exit_reason, resolved_algo_id,
             resolved_metadata_source, reconciled_at_ms)
            VALUES
            (50, 1000, '', 'SOLUSDT', '50', '500', '', 'sell', 2, 72, 144,
             0.1, 'USDT', 0, 'taker', 0, 'external', '', 'rest', '{}',
             'external', 'manual', 0, 'MARKET', 'MANUAL_CLOSE', '', 'order', 1)
            """
        )
        con.execute(
            """
            INSERT INTO binance_trade_cycle_fills
            (run_id, cycle_id, exchange_fill_id, role, qty, price, fee,
             realized_pnl, fill_ownership, exit_reason)
            VALUES (1, 10, 50, 'ENTRY', 2, 72, 0.1, 0, 'external', '')
            """
        )
        con.commit()
    finally:
        con.close()

    result = status.search_trades(
        db, status.TradeFilters(sources=["external"], limit=20)
    )

    assert result["total"] == 1
    assert result["items"][0]["record_key"] == "binance:1:10"
    assert result["items"][0]["ownership"] == "mixed"
    assert result["items"][0]["realized_pnl"] == 2
    assert result["items"][0]["orders"][0]["fill_ownership"] == "external"


async def test_decision_detail_includes_llm_trace_and_data_items(db):
    detail = status.decision_detail(db, 3)
    assert detail is not None
    assert detail["llm_trace_available"] is True
    assert detail["llm_system_prompt"] == "stored system prompt"
    assert detail["llm_user_prompt"] == "stored prompt"
    assert "request" in detail["llm_request_effective_json"]
    fields = {item["field"] for item in detail["llm_data_items"]}
    assert {"last_price", "mark_price", "recent_klines_last20",
            "micro_klines_last30"}.issubset(fields)
    assert detail["actual_protection"]["status"] == "no_entry"


async def test_decision_detail_includes_actual_protection_prices(db):
    s = Store(db)
    await s.connect()
    await s.log_decision(
        symbol="SOLUSDT",
        decision=TradeDecision(
            symbol="SOLUSDT",
            action=Action.OPEN_SHORT,
            confidence=0.7,
            size_pct=0.1,
            leverage=3,
            stop_loss_pct=0.002,
            take_profit_pct=0.0045,
            reason="short setup",
        ),
        ref_price=598.61,
    )
    decision_id = status.search_decisions(
        db,
        status.DecisionFilters(symbols=["SOLUSDT"], types=["OPEN_SHORT"], limit=1),
    )["items"][0]["id"]
    opened = await s.log_order({
        "symbol": "SOLUSDT", "kind": "OPEN", "side": "sell",
        "order_type": "market", "qty": 3.13, "price": 598.58,
        "notional": 1873.56, "dry_run": False, "status": "filled",
        "id": "open-sol", "raw": {}, "leverage": 3,
    })
    old_sl = await s.log_order({
        "symbol": "SOLUSDT", "kind": "SL", "side": "buy",
        "order_type": "STOP_MARKET", "qty": 3.13, "price": 599.78,
        "notional": 1877.31, "dry_run": False, "status": "canceled",
        "id": "old-sl-sol", "raw": {}, "trade_id": opened["trade_id"],
    })
    tp = await s.log_order({
        "symbol": "SOLUSDT", "kind": "TP", "side": "buy",
        "order_type": "TAKE_PROFIT_MARKET", "qty": 3.13, "price": 595.89,
        "notional": 1865.14, "dry_run": False, "status": "placed",
        "id": "tp-sol", "raw": {}, "trade_id": opened["trade_id"],
    })
    await s.log_decision(
        symbol="SOLUSDT",
        decision=TradeDecision(
            symbol="SOLUSDT",
            action=Action.HOLD,
            confidence=0.4,
            size_pct=0.0,
            leverage=1,
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            reason="hold",
        ),
        ref_price=599.0,
    )
    next_decision_id = status.search_decisions(
        db,
        status.DecisionFilters(symbols=["SOLUSDT"], types=["HOLD"], limit=1),
    )["items"][0]["id"]
    repaired_sl = await s.log_order({
        "symbol": "SOLUSDT", "kind": "SL", "side": "buy",
        "order_type": "STOP_MARKET", "qty": 3.13, "price": 599.78,
        "notional": 1877.31, "dry_run": False, "status": "placed",
        "id": "new-sl-sol", "raw": {}, "trade_id": opened["trade_id"],
    })
    await s.close()

    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE decisions SET ts_ms = 1000 WHERE id = ?", (decision_id,))
        conn.execute("UPDATE decisions SET ts_ms = 2000 WHERE id = ?", (next_decision_id,))
        conn.execute("UPDATE orders SET ts_ms = 1500 WHERE id = ?", (opened["order_id"],))
        conn.execute("UPDATE orders SET ts_ms = 1600 WHERE id = ?", (old_sl["order_id"],))
        conn.execute("UPDATE orders SET ts_ms = 1700 WHERE id = ?", (tp["order_id"],))
        conn.execute("UPDATE orders SET ts_ms = 5000 WHERE id = ?", (repaired_sl["order_id"],))
        conn.commit()
    finally:
        conn.close()

    detail = status.decision_detail(db, decision_id)
    protection = detail["actual_protection"]
    assert protection["status"] == "complete"
    assert protection["entry"]["price"] == pytest.approx(598.58)
    assert protection["entry"]["trade_id"] == opened["trade_id"]
    assert protection["sl"]["price"] == pytest.approx(599.78)
    assert protection["sl"]["status"] == "placed"
    assert protection["sl"]["exchange_order_id"] == "new-sl-sol"
    assert protection["tp"]["price"] == pytest.approx(595.89)
    assert protection["tp"]["status"] == "placed"
    assert protection["expected"] == {"sl": True, "tp": True}


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


def test_utc8_day_start_ms():
    now_ms = int(datetime(2025, 12, 14, 9, tzinfo=timezone.utc).timestamp() * 1000)
    expected = int(datetime(2025, 12, 13, 16, tzinfo=timezone.utc).timestamp() * 1000)
    assert status.utc8_day_start_ms(now_ms) == expected


async def test_day_equity_change_uses_utc8_day_boundary(db, monkeypatch):
    s = Store(db)
    await s.connect()
    rt = RuntimeState()
    await s.snapshot_balance(total_equity=205.0, available_margin=180.0, runtime=rt)
    await s.snapshot_balance(total_equity=197.5, available_margin=180.0, runtime=rt)
    await s.close()

    now_ms = int(datetime(2025, 12, 14, 9, tzinfo=timezone.utc).timestamp() * 1000)
    day_start = status.utc8_day_start_ms(now_ms)
    conn = sqlite3.connect(db)
    try:
        ids = [row[0] for row in conn.execute("SELECT id FROM balance_snapshots ORDER BY id")]
        timestamps = [day_start - 1_000, day_start + 1_000, day_start + 2_000]
        for ts, row_id in zip(timestamps, ids, strict=True):
            conn.execute("UPDATE balance_snapshots SET ts_ms = ? WHERE id = ?", (ts, row_id))
        conn.commit()
    finally:
        conn.close()

    change = status.day_equity_change(db, latest_ts_ms=day_start + 2_000)
    assert change["day_equity_start_ts_ms"] == day_start
    assert change["day_equity_start_snapshot_ts_ms"] == day_start - 1_000
    assert change["day_equity_start"] == pytest.approx(200.0)
    assert change["day_equity_latest"] == pytest.approx(197.5)
    assert change["day_equity_change"] == pytest.approx(-2.5)
    monkeypatch.setattr(status, "_now_ms", lambda: day_start + 2_000)
    assert status.latest_balance(db)["day_equity_change"] == pytest.approx(-2.5)
    assert status.pnl_stats(db)["day_equity_change"] == pytest.approx(-2.5)


async def test_day_equity_change_accepts_live_equity(db):
    latest_ts = status.latest_balance(db)["ts_ms"]
    change = status.day_equity_change(
        db,
        current_equity=193.25,
        now_ms=latest_ts,
    )
    assert change["day_equity_start"] == pytest.approx(200.0)
    assert change["day_equity_latest"] == pytest.approx(193.25)
    assert change["day_equity_change"] == pytest.approx(-6.75)


def test_resolve_time_bounds_custom_range():
    start, end, key = status.resolve_time_bounds(
        start_ts_ms=1000,
        end_ts_ms=2000,
        range_key="1h",
    )
    assert key == "custom"
    assert start == 1000
    assert end == 2000


async def test_recent_commands_exposes_utc_timestamp_ms(db):
    s = Store(db)
    await s.connect()
    command_id = await s.enqueue_command("PAUSE", source="test")
    await s.close()

    executed_at = datetime(2025, 12, 14, 1, tzinfo=timezone.utc)
    executed_at_ms = int(executed_at.timestamp() * 1000)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE control_commands SET ts_ms = ?, executed_at = ? WHERE id = ?",
            (executed_at_ms, "2025-12-14 01:00:00", command_id),
        )
        conn.commit()
    finally:
        conn.close()

    command = status.recent_commands(db, 1)[0]
    assert command["created_at_ms"] == executed_at_ms
    assert command["executed_at_ms"] == executed_at_ms


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
    assert res["items"][0]["llm_prompt"] == "stored prompt"
    assert "request" in res["items"][0]["llm_request_json"]

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


async def test_search_trades_combines_external_records_without_changing_strategy_rows(db):
    s = Store(db)
    await s.connect()
    await s.ingest_exchange_fill({
        "symbol": "ETHUSDT",
        "exchange_trade_id": "external-1",
        "exchange_order_id": "manual-order-1",
        "client_order_id": "web_manual_1",
        "side": "buy",
        "qty": 0.5,
        "price": 2000.0,
        "ts_ms": 9_999_999_999_999,
        "fee": 0.2,
        "fee_asset": "USDT",
        "liquidity": "taker",
        "source": "rest",
    })
    await s.close()

    combined = status.search_trades(db, status.TradeFilters())
    assert combined["total"] == 2
    assert {row["record_type"] for row in combined["items"]} == {"strategy", "external"}
    external = next(row for row in combined["items"] if row["record_type"] == "external")
    assert external["source_label"] == "Binance 外部/手工交易"
    assert external["record_key"].startswith("external:")
    assert external["orders"][0]["client_kind"] == "OPEN"

    strategy_only = status.search_trades(
        db, status.TradeFilters(sources=["strategy"])
    )
    assert strategy_only["total"] == 1
    assert strategy_only["items"][0]["record_type"] == "strategy"


async def test_missing_table_degrades_to_empty(tmp_path):
    # 全新空库（无表）→ 查询不应抛错，返回空
    empty = str(tmp_path / "empty.db")
    import sqlite3
    sqlite3.connect(empty).close()  # 建立空文件
    assert status.recent_orders(empty) == []
    assert status.latest_balance(empty) is None


async def test_decision_detail_exposes_latency_fields(db):
    s = Store(db)
    await s.connect()
    d = TradeDecision(
        symbol="ETHUSDT",
        action=Action.OPEN_LONG,
        confidence=0.8,
        size_pct=0.1,
        leverage=3,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        reason="trend up",
    )
    await s.log_decision(
        symbol="ETHUSDT",
        decision=d,
        ref_price=3000.0,
        llm_latency_ms=7821,
        llm_attempts=2,
        llm_status="ok",
        llm_error="",
    )
    await s.close()
    detail = status.decision_detail(db, status.search_decisions(db, status.DecisionFilters(types=["OPEN_LONG"], limit=1))["items"][0]["id"])
    assert detail is not None
    assert detail["llm_latency_ms"] == 7821
    assert detail["llm_attempts"] == 2
    assert detail["llm_status"] == "ok"
    assert detail["llm_status_available"] is True


async def test_decision_detail_zero_latency_marks_unavailable(db):
    detail = status.decision_detail(db, status.search_decisions(db, status.DecisionFilters(symbols=["BNBUSDT"], limit=1))["items"][0]["id"])
    assert detail is not None
    assert detail["llm_latency_ms"] == 0
    assert detail["llm_status_available"] is False


async def test_search_decisions_hides_no_significant_change(db):
    """hide_no_significant_change 应隐藏 skip_reason='no significant change' 的记录。"""
    s = Store(db)
    await s.connect()
    await s.log_decision(
        symbol="BNBUSDT", skipped=True, skip_reason="no significant change", ref_price=600.0,
    )
    await s.close()

    res = status.search_decisions(db, status.DecisionFilters(hide_no_significant_change=True))
    # 原始 db 已有 3 条；插入 1 条 no_significant_change 后变 4 条；过滤后剩 3 条。
    assert res["total"] == 3
    assert all(row.get("skip_reason") != "no significant change" for row in res["items"])

    # 与 hide_symbol_disabled 一起勾选时，两类 skip_reason 都被过滤
    res2 = status.search_decisions(
        db,
        status.DecisionFilters(hide_symbol_disabled=True, hide_no_significant_change=True),
    )
    assert all(
        row.get("skip_reason") not in ("symbol disabled", "no significant change")
        for row in res2["items"]
    )


async def test_default_filters_hide_noisy_skip_reasons(db):
    """前端默认行为：两个 hide_* 都为 true，应同时过滤两类 skip 日志。"""
    s = Store(db)
    await s.connect()
    await s.log_decision(
        symbol="BNBUSDT", skipped=True, skip_reason="no significant change", ref_price=600.0,
    )
    await s.close()

    res = status.search_decisions(
        db,
        status.DecisionFilters(hide_symbol_disabled=True, hide_no_significant_change=True),
    )
    assert all(
        row.get("skip_reason") not in ("symbol disabled", "no significant change")
        for row in res["items"]
    )
