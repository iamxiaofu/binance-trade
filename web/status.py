"""只读状态查询：从 SQLite 读取最近的决策/订单/快照，供前端可视化消费。

刻意保持「只读 + 无额外依赖」：用同步 sqlite3 直接查库，不与交易主进程共享连接，
也不引入 web 框架。后期接 FastAPI/前端时，HTTP 层调用这里的函数即可。

注意：这是观测面，绝不写库、绝不触碰交易所。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3
import time
from typing import Any

from loguru import logger

from src.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from src.llm.schema import MarketContext, TradeDecision
from src.throttle.gate import NO_SIGNIFICANT_CHANGE_REASON as _NO_SIGNIFICANT_CHANGE


_SLOW_DECISION_QUERY_MS = 500.0
_SLOW_TRADE_QUERY_MS = 500.0
_SUMMARY_RECENT_DECISION_LIMIT = 5


def _rows(db_path: str, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError as e:
        # 交易进程尚未建表（DB 为空）时优雅降级为空结果，而非 500。
        if "no such table" in str(e):
            return []
        raise
    finally:
        conn.close()


_OPEN_DECISION_ORDER_WINDOW_MS = 15 * 60 * 1000


def _order_public_fields(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    keys = (
        "id", "ts_ms", "created_at", "symbol", "client_kind", "side",
        "order_type", "qty", "price", "status", "exchange_order_id",
        "trade_id",
    )
    return {key: row.get(key) for key in keys}


def _decision_actual_protection(db_path: str, row: dict[str, Any]) -> dict[str, Any]:
    """Return actual post-fill entry/SL/TP rows derived from orders.

    The decisions table intentionally stores LLM intent only. Actual protection
    prices live in orders.price for SL/TP rows, because that is the stopPrice
    sent to the exchange.
    """
    action = str(row.get("action") or "").upper()
    if action not in ("OPEN_LONG", "OPEN_SHORT"):
        return {
            "status": "not_applicable",
            "message": "非开仓决策",
            "entry": None,
            "sl": None,
            "tp": None,
            "expected": {"sl": False, "tp": False},
        }

    symbol = str(row.get("symbol") or "")
    decision_ts = int(row.get("ts_ms") or 0)
    decision_id = int(row.get("id") or 0)
    try:
        tp_plan = json.loads(str(row.get("take_profit_plan_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        tp_plan = []
    expected = {
        "sl": float(row.get("stop_loss_pct") or 0.0) > 0,
        "tp": float(row.get("take_profit_pct") or 0.0) > 0 or bool(tp_plan),
    }
    if not symbol or decision_ts <= 0:
        return {
            "status": "no_entry",
            "message": "决策缺少 symbol/ts_ms，无法匹配成交订单",
            "entry": None,
            "sl": None,
            "tp": None,
            "expected": expected,
        }

    next_rows = _rows(
        db_path,
        "SELECT MIN(ts_ms) AS next_ts FROM decisions "
        "WHERE symbol = ? AND id > ? AND ts_ms > ?",
        (symbol, decision_id, decision_ts),
    )
    next_ts = int(next_rows[0].get("next_ts") or 0) if next_rows else 0
    upper_ts = decision_ts + _OPEN_DECISION_ORDER_WINDOW_MS
    if next_ts > decision_ts:
        upper_ts = min(upper_ts, next_ts)

    open_side = "buy" if action == "OPEN_LONG" else "sell"
    entry_rows = _rows(
        db_path,
        "SELECT * FROM orders "
        "WHERE symbol = ? AND client_kind = 'OPEN' AND side = ? "
        "AND status IN ('filled', 'partial') "
        "AND ts_ms >= ? AND ts_ms < ? "
        "ORDER BY ts_ms ASC, id ASC LIMIT 1",
        (symbol, open_side, decision_ts, upper_ts),
    )
    if not entry_rows:
        return {
            "status": "no_entry",
            "message": "未找到该决策成交后的 OPEN 订单",
            "entry": None,
            "sl": None,
            "tp": None,
            "expected": expected,
        }

    entry = entry_rows[0]
    trade_id = int(entry.get("trade_id") or 0)
    if trade_id > 0:
        protection_rows = _rows(
            db_path,
            "SELECT * FROM orders "
            "WHERE trade_id = ? AND client_kind IN ('SL', 'TP') "
            "ORDER BY ts_ms ASC, id ASC",
            (trade_id,),
        )
    else:
        close_side = "sell" if action == "OPEN_LONG" else "buy"
        protection_rows = _rows(
            db_path,
            "SELECT * FROM orders "
            "WHERE symbol = ? AND side = ? AND client_kind IN ('SL', 'TP') "
            "AND ts_ms >= ? AND ts_ms <= ? "
            "ORDER BY ts_ms ASC, id ASC",
            (symbol, close_side, int(entry.get("ts_ms") or decision_ts), decision_ts + _OPEN_DECISION_ORDER_WINDOW_MS),
        )

    latest: dict[str, dict[str, Any]] = {}
    tp_orders: list[dict[str, Any]] = []
    for order in protection_rows:
        kind = str(order.get("client_kind") or "").upper()
        if kind in ("SL", "TP"):
            latest[kind] = order
        if kind == "TP":
            tp_orders.append(order)

    missing = []
    if expected["sl"] and "SL" not in latest:
        missing.append("SL")
    if expected["tp"] and "TP" not in latest:
        missing.append("TP")
    status = "missing" if missing else "complete"
    message = "" if not missing else "缺少 " + "/".join(missing)
    return {
        "status": status,
        "message": message,
        "entry": _order_public_fields(entry),
        "sl": _order_public_fields(latest.get("SL")),
        "tp": _order_public_fields(latest.get("TP")),
        "tp_orders": [_order_public_fields(order) for order in tp_orders],
        "expected": expected,
    }


def recent_decisions(db_path: str, limit: int = 50) -> list[dict]:
    return _rows(db_path, "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))


# 集中管理 “跳过 LLM” 的原因字符串，避免前后端 SQL 拼接错位。
SYMBOL_DISABLED_REASON = "symbol disabled"
NO_SIGNIFICANT_CHANGE_REASON = _NO_SIGNIFICANT_CHANGE


@dataclass(frozen=True)
class DecisionFilters:
    symbols: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    start_ts_ms: int | None = None
    end_ts_ms: int | None = None
    hide_symbol_disabled: bool = False
    hide_no_significant_change: bool = False
    limit: int = 100
    offset: int = 0


def _decision_where(filters: DecisionFilters) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []

    symbols = [s.strip().upper() for s in filters.symbols if s and s.strip()]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        clauses.append(f"symbol IN ({placeholders})")
        args.extend(symbols)

    types = [t.strip().upper() for t in filters.types if t and t.strip()]
    action_types = [t for t in types if t != "SKIPPED"]
    include_skipped = "SKIPPED" in types
    if include_skipped and action_types:
        placeholders = ",".join("?" for _ in action_types)
        clauses.append(f"(skipped = 1 OR (skipped = 0 AND action IN ({placeholders})))")
        args.extend(action_types)
    elif include_skipped:
        clauses.append("skipped = 1")
    elif action_types:
        placeholders = ",".join("?" for _ in action_types)
        clauses.append(f"skipped = 0 AND action IN ({placeholders})")
        args.extend(action_types)

    if filters.start_ts_ms is not None:
        clauses.append("ts_ms >= ?")
        args.append(filters.start_ts_ms)
    if filters.end_ts_ms is not None:
        clauses.append("ts_ms <= ?")
        args.append(filters.end_ts_ms)
    if filters.hide_symbol_disabled:
        clauses.append("NOT (skipped = 1 AND skip_reason = ?)")
        args.append(SYMBOL_DISABLED_REASON)
    if filters.hide_no_significant_change:
        clauses.append("NOT (skipped = 1 AND skip_reason = ?)")
        args.append(NO_SIGNIFICANT_CHANGE_REASON)

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def search_decisions(db_path: str, filters: DecisionFilters) -> dict[str, Any]:
    """服务端筛选决策日志，返回分页结果。"""
    started = time.perf_counter()
    limit = max(1, min(int(filters.limit or 100), 500))
    offset = max(0, int(filters.offset or 0))
    filters = DecisionFilters(
        symbols=filters.symbols,
        types=filters.types,
        start_ts_ms=filters.start_ts_ms,
        end_ts_ms=filters.end_ts_ms,
        hide_symbol_disabled=filters.hide_symbol_disabled,
        hide_no_significant_change=filters.hide_no_significant_change,
        limit=limit,
        offset=offset,
    )
    where_sql, args = _decision_where(filters)
    items = _rows(
        db_path,
        f"SELECT * FROM decisions{where_sql} ORDER BY ts_ms DESC, id DESC LIMIT ? OFFSET ?",
        tuple(args + [limit, offset]),
    )
    total_rows = _rows(db_path, f"SELECT COUNT(*) AS total FROM decisions{where_sql}", tuple(args))
    total = int(total_rows[0]["total"]) if total_rows else 0
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms >= _SLOW_DECISION_QUERY_MS:
        logger.warning(
            "slow decision search {:.1f}ms limit={} offset={} symbols={} types={} "
            "start_ts_ms={} end_ts_ms={} hide_symbol_disabled={} hide_no_significant_change={}",
            elapsed_ms, limit, offset, filters.symbols, filters.types,
            filters.start_ts_ms, filters.end_ts_ms,
            filters.hide_symbol_disabled, filters.hide_no_significant_change,
        )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {
            "symbols": filters.symbols,
            "types": filters.types,
            "start_ts_ms": filters.start_ts_ms,
            "end_ts_ms": filters.end_ts_ms,
            "hide_symbol_disabled": filters.hide_symbol_disabled,
            "hide_no_significant_change": filters.hide_no_significant_change,
        },
    }


def recent_orders(db_path: str, limit: int = 50) -> list[dict]:
    return _rows(db_path, "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))


RANGE_MS: dict[str, int] = {
    "1h": 60 * 60 * 1000,
    "3h": 3 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
}

UTC8_OFFSET_MS = 8 * 60 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def utc8_day_start_ms(now_ms: int | None = None) -> int:
    """Return the UTC timestamp for the current UTC+8 calendar day start."""
    now = int(now_ms if now_ms is not None else _now_ms())
    return ((now + UTC8_OFFSET_MS) // DAY_MS) * DAY_MS - UTC8_OFFSET_MS


def resolve_time_bounds(
    *,
    range_key: str | None = None,
    start_ts_ms: int | None = None,
    end_ts_ms: int | None = None,
    now_ms: int | None = None,
) -> tuple[int | None, int | None, str]:
    """Resolve quick range/custom range into timestamp bounds."""
    end = end_ts_ms if end_ts_ms is not None else (now_ms or _now_ms())
    if start_ts_ms is not None:
        return start_ts_ms, end, "custom"
    key = (range_key or "").strip().lower()
    if key in RANGE_MS:
        return end - RANGE_MS[key], end, key
    return None, end_ts_ms, ""


def _ts_where(
    column: str,
    *,
    start_ts_ms: int | None = None,
    end_ts_ms: int | None = None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if start_ts_ms is not None:
        clauses.append(f"{column} >= ?")
        args.append(start_ts_ms)
    if end_ts_ms is not None:
        clauses.append(f"{column} <= ?")
        args.append(end_ts_ms)
    return clauses, args


def _sample_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    step = (len(rows) - 1) / (limit - 1)
    picked = []
    seen: set[int] = set()
    for i in range(limit):
        idx = round(i * step)
        if idx not in seen:
            picked.append(rows[idx])
            seen.add(idx)
    return picked


@dataclass(frozen=True)
class TradeFilters:
    symbols: list[str] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    exit_reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    start_ts_ms: int | None = None
    end_ts_ms: int | None = None
    limit: int = 100
    offset: int = 0


def _trade_where(filters: TradeFilters) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []

    symbols = [s.strip().upper() for s in filters.symbols if s and s.strip()]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        clauses.append(f"symbol IN ({placeholders})")
        args.extend(symbols)

    directions = [d.strip().lower() for d in filters.directions if d and d.strip()]
    if directions:
        placeholders = ",".join("?" for _ in directions)
        clauses.append(f"direction IN ({placeholders})")
        args.extend(directions)

    statuses = [s.strip().lower() for s in filters.statuses if s and s.strip()]
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        args.extend(statuses)

    exit_reasons = [r.strip().upper() for r in filters.exit_reasons if r and r.strip()]
    if exit_reasons:
        placeholders = ",".join("?" for _ in exit_reasons)
        clauses.append(f"exit_reason IN ({placeholders})")
        args.extend(exit_reasons)

    if filters.start_ts_ms is not None:
        clauses.append("opened_at_ms >= ?")
        args.append(filters.start_ts_ms)
    if filters.end_ts_ms is not None:
        clauses.append("opened_at_ms <= ?")
        args.append(filters.end_ts_ms)

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def search_trades(db_path: str, filters: TradeFilters) -> dict[str, Any]:
    """Return strategy and external Binance trade lifecycles without mixing storage."""
    started = time.perf_counter()
    limit = max(1, min(int(filters.limit or 100), 500))
    offset = max(0, int(filters.offset or 0))
    filters = TradeFilters(
        symbols=filters.symbols,
        directions=filters.directions,
        statuses=filters.statuses,
        exit_reasons=filters.exit_reasons,
        sources=filters.sources,
        start_ts_ms=filters.start_ts_ms,
        end_ts_ms=filters.end_ts_ms,
        limit=limit,
        offset=offset,
    )
    where_sql, args = _trade_where(filters)
    order_count = 0
    requested_sources = {str(value).strip().lower() for value in filters.sources if value}
    include_strategy = not requested_sources or "strategy" in requested_sources
    include_external = not requested_sources or "external" in requested_sources
    fetch_limit = offset + limit

    strategy_items = []
    strategy_total = 0
    if include_strategy:
        strategy_items = _rows(
            db_path,
            f"SELECT * FROM trades{where_sql} "
            "ORDER BY opened_at_ms DESC, id DESC LIMIT ?",
            tuple(args + [fetch_limit]),
        )
        total_rows = _rows(
            db_path, f"SELECT COUNT(*) AS total FROM trades{where_sql}", tuple(args)
        )
        strategy_total = int(total_rows[0]["total"]) if total_rows else 0
        for item in strategy_items:
            item.update({
                "record_key": f"strategy:{item['id']}",
                "record_type": "strategy",
                "ownership": "engine",
                "source_label": "Engine 策略交易",
                "classification_reason": "",
            })

    external_items = []
    external_total = 0
    active_run_id = _active_binance_trade_run(db_path)
    canonical_external = bool(
        active_run_id and _table_exists(db_path, "binance_trade_cycles")
    )
    if include_external and (canonical_external or _table_exists(db_path, "external_trades")):
        if canonical_external:
            run_clause = "run_id = ?"
            combined_where = (
                f" WHERE {run_clause} AND {where_sql[7:]}"
                if where_sql else f" WHERE {run_clause}"
            )
            external_items = _rows(
                db_path,
                "SELECT *, gross_realized_pnl AS realized_pnl, "
                "0 AS leverage, 0.0 AS entry_margin, "
                "'binance_reconciled' AS source "
                f"FROM binance_trade_cycles{combined_where} "
                "ORDER BY opened_at_ms DESC, id DESC LIMIT ?",
                tuple([active_run_id] + args + [fetch_limit]),
            )
            total_rows = _rows(
                db_path,
                f"SELECT COUNT(*) AS total FROM binance_trade_cycles{combined_where}",
                tuple([active_run_id] + args),
            )
        else:
            external_items = _rows(
                db_path,
                f"SELECT * FROM external_trades{where_sql} "
                "ORDER BY opened_at_ms DESC, id DESC LIMIT ?",
                tuple(args + [fetch_limit]),
            )
            total_rows = _rows(
                db_path,
                f"SELECT COUNT(*) AS total FROM external_trades{where_sql}",
                tuple(args),
            )
        external_total = int(total_rows[0]["total"]) if total_rows else 0
        for item in external_items:
            item.update({
                "record_key": (
                    f"binance:{active_run_id}:{item['id']}"
                    if canonical_external else f"external:{item['id']}"
                ),
                "record_type": "external",
                "source_label": (
                    "Binance 外部/手工交易（窗口前持仓）"
                    if item.get("confidence") == "carry_in"
                    else (
                        "Binance 外部/Engine 混合交易"
                        if item.get("ownership") == "mixed"
                        else "Binance 外部/手工交易"
                    )
                ),
            })
            item.setdefault("ownership", "external")

    items = sorted(
        strategy_items + external_items,
        key=lambda row: (int(row.get("opened_at_ms") or 0), int(row.get("id") or 0)),
        reverse=True,
    )[offset:offset + limit]
    total = strategy_total + external_total

    strategy_ids = [
        int(row["id"]) for row in items if row.get("record_type") == "strategy"
    ]
    if strategy_ids:
        placeholders = ",".join("?" for _ in strategy_ids)
        order_rows = _rows(
            db_path,
            f"SELECT * FROM orders WHERE trade_id IN ({placeholders}) ORDER BY trade_id, ts_ms, id",
            tuple(strategy_ids),
        )
        order_count = len(order_rows)
        by_trade: dict[int, list[dict[str, Any]]] = {}
        for row in order_rows:
            by_trade.setdefault(int(row.get("trade_id") or 0), []).append(row)
        for item in items:
            if item.get("record_type") == "strategy":
                item["orders"] = by_trade.get(int(item["id"]), [])

    external_ids = [
        int(row["id"]) for row in items if row.get("record_type") == "external"
    ]
    if external_ids:
        placeholders = ",".join("?" for _ in external_ids)
        if canonical_external:
            fill_rows = _rows(
                db_path,
                f"""
                SELECT bcf.cycle_id AS trade_id, ef.ts_ms, ef.created_at,
                       ef.symbol,
                       CASE WHEN bcf.role IN ('ENTRY', 'REVERSAL_ENTRY') THEN 'OPEN'
                            ELSE 'CLOSE' END AS client_kind,
                       ef.side,
                       COALESCE(NULLIF(ef.resolved_order_type, ''), 'trade') AS order_type,
                       bcf.qty, bcf.price, ABS(bcf.qty * bcf.price) AS notional,
                       'filled' AS status, ef.exchange_order_id,
                       bcf.role AS trade_role, 0 AS margin,
                       bcf.realized_pnl, '' AS execution_mode,
                       ef.liquidity, bcf.fee, ef.fee_asset,
                       COALESCE(NULLIF(ef.resolved_client_order_id, ''), ef.client_order_id)
                           AS client_order_id,
                       ef.raw_json, bcf.fill_ownership, bcf.exit_reason
                FROM binance_trade_cycle_fills bcf
                JOIN exchange_fills ef ON ef.id = bcf.exchange_fill_id
                WHERE bcf.run_id = ? AND bcf.cycle_id IN ({placeholders})
                ORDER BY bcf.cycle_id, ef.ts_ms, bcf.id
                """,
                tuple([active_run_id] + external_ids),
            )
        else:
            fill_rows = _rows(
                db_path,
                f"""
                SELECT etf.external_trade_id AS trade_id, ef.ts_ms, ef.created_at,
                       ef.symbol,
                       CASE WHEN etf.role IN ('ENTRY', 'REVERSAL_ENTRY') THEN 'OPEN'
                            ELSE 'CLOSE' END AS client_kind,
                       ef.side, 'trade' AS order_type, etf.qty,
                       etf.price, ABS(etf.qty * etf.price) AS notional,
                       'filled' AS status, ef.exchange_order_id,
                       etf.role AS trade_role, 0 AS margin,
                       etf.realized_pnl, '' AS execution_mode,
                       ef.liquidity, etf.fee, ef.fee_asset,
                       ef.client_order_id, ef.raw_json
                FROM external_trade_fills etf
                JOIN exchange_fills ef ON ef.id = etf.exchange_fill_id
                WHERE etf.external_trade_id IN ({placeholders})
                ORDER BY etf.external_trade_id, ef.ts_ms, etf.id
                """,
                tuple(external_ids),
            )
        order_count += len(fill_rows)
        by_external: dict[int, list[dict[str, Any]]] = {}
        for row in fill_rows:
            by_external.setdefault(int(row.get("trade_id") or 0), []).append(row)
        for item in items:
            if item.get("record_type") == "external":
                item["orders"] = by_external.get(int(item["id"]), [])
                item.setdefault("dry_run", False)
                item.setdefault("entry_order_id", 0)
                item.setdefault("exit_order_id", 0)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms >= _SLOW_TRADE_QUERY_MS:
        logger.warning(
            "slow trade search {:.1f}ms limit={} offset={} total={} rows={} orders={} "
            "symbols={} directions={} statuses={} exit_reasons={} sources={} "
            "start_ts_ms={} end_ts_ms={}",
            elapsed_ms, limit, offset, total, len(items), order_count,
            filters.symbols, filters.directions, filters.statuses, filters.exit_reasons,
            filters.sources,
            filters.start_ts_ms, filters.end_ts_ms,
        )
    audit = {"external": 0, "mixed": 0, "unknown": 0}
    if _table_exists(db_path, "exchange_fills"):
        audit_rows = _rows(
            db_path,
            "SELECT COALESCE(NULLIF(resolved_ownership, ''), ownership) AS ownership, "
            "COUNT(*) AS total FROM exchange_fills "
            "WHERE COALESCE(NULLIF(resolved_ownership, ''), ownership) "
            "IN ('external', 'mixed', 'unknown') GROUP BY 1",
        )
        for row in audit_rows:
            audit[str(row.get("ownership") or "")] = int(row.get("total") or 0)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {
            "symbols": filters.symbols,
            "directions": filters.directions,
            "statuses": filters.statuses,
            "exit_reasons": filters.exit_reasons,
            "sources": filters.sources,
            "start_ts_ms": filters.start_ts_ms,
            "end_ts_ms": filters.end_ts_ms,
        },
        "fill_audit": audit,
    }


def _table_exists(db_path: str, table_name: str) -> bool:
    rows = _rows(
        db_path,
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return bool(rows)


def _active_binance_trade_run(db_path: str) -> int:
    if not _table_exists(db_path, "runtime_settings"):
        return 0
    rows = _rows(
        db_path,
        "SELECT value FROM runtime_settings WHERE key = ? LIMIT 1",
        ("binance.trade_cycles.active_run_id",),
    )
    try:
        return int(rows[0]["value"]) if rows else 0
    except (TypeError, ValueError):
        return 0


def recent_rejects(db_path: str, limit: int = 50) -> list[dict]:
    return _rows(db_path, "SELECT * FROM rejects ORDER BY id DESC LIMIT ?", (limit,))


def latest_positions(db_path: str) -> list[dict]:
    """每个 symbol 的最新一条非零持仓快照。"""
    return _rows(
        db_path,
        """
        SELECT p.* FROM position_snapshots p
        JOIN (SELECT symbol, MAX(id) AS mid FROM position_snapshots GROUP BY symbol) m
          ON p.id = m.mid
        WHERE ABS(COALESCE(p.contracts, 0)) > 0
        ORDER BY p.symbol
        """,
    )


def open_trade_metadata(db_path: str) -> dict[str, dict]:
    """当前 open/partial trade 的本地开仓元数据，供实时持仓展示补充。"""
    rows = _rows(
        db_path,
        """
        SELECT id, symbol, opened_at_ms, opened_at, source, confidence, leverage,
               qty_opened, qty_closed
        FROM trades
        WHERE status IN ('open', 'partial')
        ORDER BY opened_at_ms ASC, id ASC
        """,
    )
    out: dict[str, dict] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        item = out.setdefault(
            symbol,
            {
                "local_opened_at_ms": row.get("opened_at_ms") or 0,
                "local_opened_at": row.get("opened_at") or "",
                "local_trade_source": row.get("source") or "",
                "local_trade_confidence": row.get("confidence") or "",
                "local_trade_qty": 0.0,
                "local_trade_count": 0,
                "local_leverage": row.get("leverage") or 0,
            },
        )
        item["local_trade_qty"] += max(
            float(row.get("qty_opened") or 0.0) - float(row.get("qty_closed") or 0.0),
            0.0,
        )
        item["local_trade_count"] += 1
        if not item.get("local_leverage") and row.get("leverage"):
            item["local_leverage"] = row.get("leverage")
    return out


def latest_balance(db_path: str) -> dict | None:
    rows = _rows(db_path, "SELECT * FROM balance_snapshots ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    latest = rows[0]
    latest.update(day_equity_change(db_path))
    return latest


def day_equity_change(
    db_path: str,
    *,
    current_equity: float | None = None,
    latest_ts_ms: int | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Account-equity delta since UTC+8 midnight.

    This is intentionally based on balance_snapshots.total_equity rather than
    realized PnL fields, so it includes fees, funding, and live unrealized PnL
    reflected in the exchange account equity.
    """
    latest_rows = _rows(
        db_path,
        "SELECT ts_ms, created_at, total_equity FROM balance_snapshots "
        "ORDER BY id DESC LIMIT 1",
    )
    if not latest_rows:
        return {
            "day_equity_change": 0.0,
            "day_equity_start": None,
            "day_equity_latest": None,
            "day_equity_start_ts_ms": utc8_day_start_ms(now_ms),
            "day_equity_start_snapshot_ts_ms": None,
            "day_equity_start_snapshot_at": "",
        }

    latest = latest_rows[0]
    effective_now = int(now_ms if now_ms is not None else (latest_ts_ms or _now_ms()))
    start_ts = utc8_day_start_ms(effective_now)
    baseline_rows = _rows(
        db_path,
        "SELECT ts_ms, created_at, total_equity FROM balance_snapshots "
        "WHERE ts_ms <= ? ORDER BY ts_ms DESC, id DESC LIMIT 1",
        (start_ts,),
    )
    if not baseline_rows:
        baseline_rows = _rows(
            db_path,
            "SELECT ts_ms, created_at, total_equity FROM balance_snapshots "
            "WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms ASC, id ASC LIMIT 1",
            (start_ts, effective_now),
        )
    latest_equity = (
        float(current_equity)
        if current_equity is not None
        else float(latest.get("total_equity") or 0.0)
    )
    baseline = baseline_rows[0] if baseline_rows else latest
    start_equity = float(baseline.get("total_equity") or 0.0)
    start_snapshot_ts = int(baseline.get("ts_ms") or 0) or None
    start_snapshot_at = str(baseline.get("created_at") or "")
    return {
        "day_equity_change": latest_equity - start_equity,
        "day_equity_start": start_equity,
        "day_equity_latest": latest_equity,
        "day_equity_start_ts_ms": start_ts,
        "day_equity_start_snapshot_ts_ms": start_snapshot_ts,
        "day_equity_start_snapshot_at": start_snapshot_at,
    }


def balance_history(
    db_path: str,
    limit: int = 500,
    *,
    start_ts_ms: int | None = None,
    end_ts_ms: int | None = None,
) -> list[dict]:
    """权益曲线数据：按时间升序的余额快照。"""
    limit = max(1, min(int(limit or 500), 2000))
    cols = (
        "SELECT ts_ms, created_at, total_equity, available_margin, "
        "day_realized_pnl, drawdown_pct, net_capital_flow, risk_equity, "
        "risk_day_drawdown_pct, capital_flow_status FROM balance_snapshots"
    )
    clauses, args = _ts_where(
        "ts_ms",
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
    )
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    if clauses:
        rows = _rows(
            db_path,
            f"{cols}{where_sql} ORDER BY ts_ms ASC, id ASC",
            tuple(args),
        )
        return _sample_rows(rows, limit)
    rows = _rows(
        db_path,
        f"{cols} ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return list(reversed(rows))


def recent_commands(db_path: str, limit: int = 50) -> list[dict]:
    rows = _rows(db_path, "SELECT * FROM control_commands ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        row["created_at_ms"] = int(row.get("ts_ms") or 0) or None
        executed_at = str(row.get("executed_at") or "").strip()
        try:
            executed_dt = datetime.strptime(executed_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            row["executed_at_ms"] = int(executed_dt.timestamp() * 1000)
        except ValueError:
            row["executed_at_ms"] = None
    return rows


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _context_from_json(context_json: str) -> MarketContext | None:
    if not context_json:
        return None
    try:
        return MarketContext.model_validate_json(context_json)
    except Exception:
        return None


def _reconstructed_request_json(user_prompt: str) -> str:
    if not user_prompt:
        return ""
    schema = TradeDecision.model_json_schema()
    schema.pop("title", None)
    return json.dumps(
        {
            "reconstructed": True,
            "note": "历史记录未保存原始请求；此处由 context_json 按当前 prompt 模板重建。",
            "system": SYSTEM_PROMPT,
            "tools": [{
                "name": "submit_decision",
                "description": "提交本周期对该标的的结构化交易决策。必须调用本工具。",
                "input_schema": schema,
            }],
            "tool_choice": {"type": "tool", "name": "submit_decision"},
            "messages": [{"role": "user", "content": user_prompt}],
        },
        ensure_ascii=False,
    )


def _add_llm_item(items: list[dict[str, str]], category: str, field: str,
                  value: Any, note: str = "") -> None:
    items.append({
        "category": category,
        "field": field,
        "value": _json_text(value),
        "note": note,
    })


def _llm_data_items(ctx: MarketContext | None) -> list[dict[str, str]]:
    """把发送给 LLM prompt 的核心数据拆成表格行。"""
    if ctx is None:
        return []
    items: list[dict[str, str]] = []

    _add_llm_item(items, "基础行情", "symbol", ctx.symbol)
    _add_llm_item(items, "基础行情", "timestamp", ctx.timestamp)
    _add_llm_item(items, "基础行情", "last_price", ctx.last_price)
    _add_llm_item(items, "基础行情", "mark_price", ctx.mark_price)
    _add_llm_item(items, "基础行情", "funding_rate", ctx.funding_rate)
    _add_llm_item(items, "基础行情", "change_24h_pct", ctx.change_24h_pct)

    _add_llm_item(items, "账户风控", "account_equity", ctx.account_equity)
    _add_llm_item(items, "账户风控", "available_margin", ctx.available_margin)
    _add_llm_item(items, "账户风控", "max_leverage_allowed", ctx.max_leverage_allowed)
    _add_llm_item(items, "账户风控", "max_order_margin_abs", ctx.max_order_margin_abs)
    _add_llm_item(items, "账户风控", "max_loss_per_trade_abs", ctx.max_loss_per_trade_abs)

    pos = ctx.position
    _add_llm_item(items, "持仓", "has_position", pos.has_position)
    _add_llm_item(items, "持仓", "side", pos.side)
    _add_llm_item(items, "持仓", "entry_price", pos.entry_price)
    _add_llm_item(items, "持仓", "size", pos.size)
    _add_llm_item(items, "持仓", "unrealized_pnl_pct", pos.unrealized_pnl_pct)
    _add_llm_item(items, "持仓", "current_leverage", pos.current_leverage)

    sentiment = ctx.sentiment
    if sentiment is not None:
        for key, value in sentiment.model_dump(mode="json").items():
            _add_llm_item(items, "市场情绪", key, value)

    for key, value in ctx.indicators.model_dump(mode="json").items():
        _add_llm_item(items, "主周期指标", key, value)

    for tf in ctx.higher_timeframes:
        prefix = f"higher_timeframes[{tf.timeframe}]"
        for key, value in tf.model_dump(mode="json").items():
            _add_llm_item(items, "多周期指标", f"{prefix}.{key}", value)

    main_count = ctx.prompt_kline_count
    recent = ctx.recent_klines[-main_count:]
    _add_llm_item(
        items,
        "K线",
        f"recent_klines_last{main_count}",
        [[round(float(x), 4) for x in k] for k in recent],
        f"prompt 中发送最近 {main_count} 根；完整窗口仅用于本地指标计算。",
    )
    _add_llm_item(items, "K线", "recent_klines_context_count", len(ctx.recent_klines),
                  "context_json 中保存的完整窗口根数。")
    micro_count = ctx.micro_kline_count
    micro = ctx.micro_klines[-micro_count:] if micro_count > 0 else []
    _add_llm_item(items, "微观K线", "micro_kline_interval", ctx.micro_kline_interval)
    _add_llm_item(
        items,
        "微观K线",
        f"micro_klines_last{micro_count}",
        [[round(float(x), 4) for x in k] for k in micro],
        f"prompt 中发送最近 {micro_count} 根短周期 K 线；用于观察最近入场节奏。",
    )
    _add_llm_item(items, "微观K线", "micro_klines_context_count", len(ctx.micro_klines))

    # 决策态上下文里只关心这些字段是否存在；如果未来把耗时纳入 MarketContext
    # 也可在此展示，本次仍以下方 decision_detail 的派生字段为准。
    return items


def decision_detail(db_path: str, decision_id: int) -> dict | None:
    """单条决策完整记录（含喂给 LLM 的 context_json）。"""
    rows = _rows(db_path, "SELECT * FROM decisions WHERE id = ?", (decision_id,))
    if not rows:
        return None
    row = rows[0]
    ctx = _context_from_json(row.get("context_json") or "")
    user_prompt = row.get("llm_prompt") or (build_user_prompt(ctx) if ctx else "")
    row["llm_system_prompt"] = row.get("llm_system_prompt") or SYSTEM_PROMPT
    row["llm_user_prompt"] = user_prompt
    row["llm_request_effective_json"] = (
        row.get("llm_request_json") or _reconstructed_request_json(user_prompt)
    )
    row["llm_response_effective_json"] = row.get("llm_response_json") or ""
    row["llm_trace_available"] = bool(row.get("llm_request_json") or row.get("llm_response_json"))
    row["llm_data_items"] = _llm_data_items(ctx)
    row["llm_latency_ms"] = int(row.get("llm_latency_ms") or 0)
    row["llm_attempts"] = int(row.get("llm_attempts") or 0)
    row["llm_status"] = row.get("llm_status") or ""
    row["llm_error"] = row.get("llm_error") or ""
    row["llm_status_available"] = bool(row["llm_status"])
    row["actual_protection"] = _decision_actual_protection(db_path, row)
    return row


@dataclass(frozen=True)
class PnlFilters:
    start_ts_ms: int | None = None
    end_ts_ms: int | None = None


def pnl_stats(db_path: str, filters: PnlFilters | None = None) -> dict:
    """盈亏统计：当日盈亏、范围内平仓笔数和交易数。

    已实现盈亏的权威值在 balance_snapshots.day_realized_pnl（运行态累计）；
    范围统计基于 orders/trades 的时间戳过滤，作为复盘视图。
    """
    filters = filters or PnlFilters()
    latest = latest_balance(db_path)
    day_pnl = latest["day_realized_pnl"] if latest else 0.0
    clauses, args = _ts_where(
        "ts_ms",
        start_ts_ms=filters.start_ts_ms,
        end_ts_ms=filters.end_ts_ms,
    )
    where_sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    closes = _rows(
        db_path,
        "SELECT symbol, notional, status, realized_pnl, fee FROM orders "
        "WHERE client_kind IN ('CLOSE','SL','TP') "
        f"AND status IN ('filled','partial'){where_sql}",
        tuple(args),
    )
    by_symbol: dict[str, int] = {}
    range_realized_pnl = 0.0
    range_fee = 0.0
    for c in closes:
        by_symbol[c["symbol"]] = by_symbol.get(c["symbol"], 0) + 1
        range_realized_pnl += float(c.get("realized_pnl") or 0.0)
        range_fee += float(c.get("fee") or 0.0)

    trade_clauses, trade_args = _ts_where(
        "opened_at_ms",
        start_ts_ms=filters.start_ts_ms,
        end_ts_ms=filters.end_ts_ms,
    )
    trade_where = (" WHERE " + " AND ".join(trade_clauses)) if trade_clauses else ""
    trade_rows = _rows(
        db_path,
        f"SELECT symbol, status FROM trades{trade_where}",
        tuple(trade_args),
    )
    trade_by_symbol: dict[str, int] = {}
    for t in trade_rows:
        trade_by_symbol[t["symbol"]] = trade_by_symbol.get(t["symbol"], 0) + 1

    return {
        "day_realized_pnl": day_pnl,
        **day_equity_change(db_path),
        "close_count": len(closes),
        "range_close_count": len(closes),
        "trade_count": len(trade_rows),
        "range_trade_count": len(trade_rows),
        "range_realized_pnl": range_realized_pnl,
        "range_fee": range_fee,
        "range_net_realized_pnl": range_realized_pnl - range_fee,
        "close_by_symbol": by_symbol,
        "trade_by_symbol": trade_by_symbol,
        "filters": {
            "start_ts_ms": filters.start_ts_ms,
            "end_ts_ms": filters.end_ts_ms,
        },
    }


def status_summary(db_path: str) -> dict:
    """聚合视图：余额 + 持仓 + 最近决策/订单。供前端首屏。"""
    return {
        "balance": latest_balance(db_path),
        "positions": latest_positions(db_path),
        "recent_decisions": recent_decisions(db_path, _SUMMARY_RECENT_DECISION_LIMIT),
        "recent_orders": recent_orders(db_path, 20),
        "recent_rejects": recent_rejects(db_path, 20),
        "recent_commands": recent_commands(db_path, 10),
    }
