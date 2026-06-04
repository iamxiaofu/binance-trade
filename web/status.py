"""只读状态查询：从 SQLite 读取最近的决策/订单/快照，供前端可视化消费。

刻意保持「只读 + 无额外依赖」：用同步 sqlite3 直接查库，不与交易主进程共享连接，
也不引入 web 框架。后期接 FastAPI/前端时，HTTP 层调用这里的函数即可。

注意：这是观测面，绝不写库、绝不触碰交易所。
"""
from __future__ import annotations

import sqlite3
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


def recent_orders(db_path: str, limit: int = 50) -> list[dict]:
    return _rows(db_path, "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))


def recent_rejects(db_path: str, limit: int = 50) -> list[dict]:
    return _rows(db_path, "SELECT * FROM rejects ORDER BY id DESC LIMIT ?", (limit,))


def latest_positions(db_path: str) -> list[dict]:
    """每个 symbol 的最新一条持仓快照。"""
    return _rows(
        db_path,
        """
        SELECT p.* FROM position_snapshots p
        JOIN (SELECT symbol, MAX(id) AS mid FROM position_snapshots GROUP BY symbol) m
          ON p.id = m.mid
        ORDER BY p.symbol
        """,
    )


def latest_balance(db_path: str) -> dict | None:
    rows = _rows(db_path, "SELECT * FROM balance_snapshots ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def balance_history(db_path: str, limit: int = 500) -> list[dict]:
    """权益曲线数据：按时间升序的余额快照。"""
    rows = _rows(
        db_path,
        "SELECT ts_ms, created_at, total_equity, available_margin, day_realized_pnl, "
        "drawdown_pct FROM balance_snapshots ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return list(reversed(rows))


def recent_commands(db_path: str, limit: int = 50) -> list[dict]:
    return _rows(db_path, "SELECT * FROM control_commands ORDER BY id DESC LIMIT ?", (limit,))


def decision_detail(db_path: str, decision_id: int) -> dict | None:
    """单条决策完整记录（含喂给 LLM 的 context_json）。"""
    rows = _rows(db_path, "SELECT * FROM decisions WHERE id = ?", (decision_id,))
    return rows[0] if rows else None


def pnl_stats(db_path: str) -> dict:
    """盈亏统计：累计/当日已实现盈亏、平仓笔数、胜率（基于 orders 的 CLOSE/SL/TP）。

    已实现盈亏的权威值在 balance_snapshots.day_realized_pnl（运行态累计）；
    这里再用平仓订单数估算笔数与胜率，作为辅助展示。
    """
    latest = latest_balance(db_path)
    day_pnl = latest["day_realized_pnl"] if latest else 0.0
    # 平仓类订单
    closes = _rows(
        db_path,
        "SELECT symbol, notional, status FROM orders "
        "WHERE client_kind IN ('CLOSE','SL','TP') AND status IN ('filled','partial','dry_run')",
    )
    by_symbol: dict[str, int] = {}
    for c in closes:
        by_symbol[c["symbol"]] = by_symbol.get(c["symbol"], 0) + 1
    return {
        "day_realized_pnl": day_pnl,
        "close_count": len(closes),
        "close_by_symbol": by_symbol,
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
