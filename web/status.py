"""只读状态查询：从 SQLite 读取最近的决策/订单/快照，供前端可视化消费。

刻意保持「只读 + 无额外依赖」：用同步 sqlite3 直接查库，不与交易主进程共享连接，
也不引入 web 框架。后期接 FastAPI/前端时，HTTP 层调用这里的函数即可。

注意：这是观测面，绝不写库、绝不触碰交易所。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
import time
from typing import Any


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


def recent_decisions(db_path: str, limit: int = 50) -> list[dict]:
    return _rows(db_path, "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))


@dataclass(frozen=True)
class DecisionFilters:
    symbols: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    start_ts_ms: int | None = None
    end_ts_ms: int | None = None
    hide_symbol_disabled: bool = False
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
        args.append("symbol disabled")

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def search_decisions(db_path: str, filters: DecisionFilters) -> dict[str, Any]:
    """服务端筛选决策日志，返回分页结果。"""
    limit = max(1, min(int(filters.limit or 100), 500))
    offset = max(0, int(filters.offset or 0))
    filters = DecisionFilters(
        symbols=filters.symbols,
        types=filters.types,
        start_ts_ms=filters.start_ts_ms,
        end_ts_ms=filters.end_ts_ms,
        hide_symbol_disabled=filters.hide_symbol_disabled,
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


def _now_ms() -> int:
    return int(time.time() * 1000)


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
    """服务端筛选交易组，返回聚合交易和明细订单。"""
    limit = max(1, min(int(filters.limit or 100), 500))
    offset = max(0, int(filters.offset or 0))
    filters = TradeFilters(
        symbols=filters.symbols,
        directions=filters.directions,
        statuses=filters.statuses,
        exit_reasons=filters.exit_reasons,
        start_ts_ms=filters.start_ts_ms,
        end_ts_ms=filters.end_ts_ms,
        limit=limit,
        offset=offset,
    )
    where_sql, args = _trade_where(filters)
    items = _rows(
        db_path,
        f"SELECT * FROM trades{where_sql} ORDER BY opened_at_ms DESC, id DESC LIMIT ? OFFSET ?",
        tuple(args + [limit, offset]),
    )
    total_rows = _rows(db_path, f"SELECT COUNT(*) AS total FROM trades{where_sql}", tuple(args))
    total = int(total_rows[0]["total"]) if total_rows else 0
    if items:
        ids = [int(row["id"]) for row in items]
        placeholders = ",".join("?" for _ in ids)
        order_rows = _rows(
            db_path,
            f"SELECT * FROM orders WHERE trade_id IN ({placeholders}) ORDER BY ts_ms, id",
            tuple(ids),
        )
        by_trade: dict[int, list[dict[str, Any]]] = {}
        for row in order_rows:
            by_trade.setdefault(int(row.get("trade_id") or 0), []).append(row)
        for item in items:
            item["orders"] = by_trade.get(int(item["id"]), [])
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
            "start_ts_ms": filters.start_ts_ms,
            "end_ts_ms": filters.end_ts_ms,
        },
    }


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


def latest_balance(db_path: str) -> dict | None:
    rows = _rows(db_path, "SELECT * FROM balance_snapshots ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


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
        "day_realized_pnl, drawdown_pct FROM balance_snapshots"
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
    return _rows(db_path, "SELECT * FROM control_commands ORDER BY id DESC LIMIT ?", (limit,))


def decision_detail(db_path: str, decision_id: int) -> dict | None:
    """单条决策完整记录（含喂给 LLM 的 context_json）。"""
    rows = _rows(db_path, "SELECT * FROM decisions WHERE id = ?", (decision_id,))
    return rows[0] if rows else None


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
        "recent_decisions": recent_decisions(db_path, 20),
        "recent_orders": recent_orders(db_path, 20),
        "recent_rejects": recent_rejects(db_path, 20),
        "recent_commands": recent_commands(db_path, 10),
    }
