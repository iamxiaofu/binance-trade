"""对账脚本：交易所今日(SHA 0:00起) vs 本地 trades 今日(closed_at_ms 本地日界)的已实现盈亏。

不做任何写入操作（read-only）。
"""
from __future__ import annotations

"""对账脚本：交易所今日(SHA 0:00起) vs 本地 trades 今日(closed_at_ms 本地日界)的已实现盈亏。

不做任何写入操作（read-only）。

用法：
    .venv/bin/python scripts/recon_day_pnl.py
    BINANCE_CONFIG=alt-config.yaml .venv/bin/python scripts/recon_day_pnl.py
"""

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

# 让脚本能 import 项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.loader import load_config  # noqa
from src.exchange.client import ExchangeClient  # noqa


def today_sha0_ms() -> int:
    """本地 (CST) 今天 00:00:00 的 epoch 毫秒。"""
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, 0)) * 1000)


def query_local(db_path: str, start_ms: int) -> dict:
    """按 closed_at_ms >= start_ms (本地) 聚合 trades.gross/net。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT symbol, direction, entry_price, qty_opened, qty_closed,
               realized_pnl, gross_realized_pnl, net_realized_pnl,
               entry_fee, exit_fee, total_fee,
               closed_at_ms,
               datetime(closed_at_ms/1000, 'unixepoch', 'localtime') AS closed_at_local
        FROM trades
        WHERE closed_at_ms >= ? AND closed_at_ms > 0
        ORDER BY closed_at_ms ASC
        """,
        (start_ms,),
    ).fetchall()
    # 顺手拉一下 trades 表里今天的所有记录（不限 closed_at_ms）看看
    all_today_rows = cur.execute(
        """
        SELECT id, symbol, status, opened_at_ms, closed_at_ms,
               net_realized_pnl,
               datetime(opened_at_ms/1000, 'unixepoch', 'localtime') AS opened_at_local,
               datetime(closed_at_ms/1000, 'unixepoch', 'localtime') AS closed_at_local
        FROM trades
        WHERE opened_at_ms >= ? OR closed_at_ms >= ?
        ORDER BY COALESCE(closed_at_ms, opened_at_ms) DESC
        LIMIT 30
        """,
        (start_ms, start_ms),
    ).fetchall()
    # 拉 balance_snapshots 最新一条的 day_realized_pnl
    snap = cur.execute(
        "SELECT ts_ms, day_realized_pnl, drawdown_pct, "
        "datetime(ts_ms/1000, 'unixepoch', 'localtime') AS ts_local "
        "FROM balance_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return {
        "rows": [dict(r) for r in rows],
        "all_today": [dict(r) for r in all_today_rows],
        "snapshot": dict(snap) if snap else None,
    }


async def fetch_exchange(symbols: list[str], start_ms: int) -> list[dict]:
    """ccxt 拉取每个 symbol 今日起所有成交 (fetch_my_trades)。"""
    settings, creds = load_config(
        os.environ.get("BINANCE_CONFIG", "config.yaml"),
        os.environ.get("BINANCE_ENV", ".env"),
    )
    cli = ExchangeClient(settings, creds)
    try:
        out = []
        for sym in symbols:
            since = start_ms
            while True:
                batch = await cli.raw.fetch_my_trades(
                    sym, since=since, limit=1000
                )
                if not batch:
                    break
                # 仅保留 today 窗口内（ccxt 偶尔会带回来略早的）
                for t in batch:
                    t["_symbol"] = sym
                    ts = int(t.get("timestamp") or 0)
                    if ts >= start_ms:
                        out.append(t)
                if len(batch) < 1000:
                    break
                # 翻页：下一批 since = 最后一条 ts + 1
                since = int(batch[-1]["timestamp"]) + 1
        return out
    finally:
        await cli.close()


def summarize(rows: list[dict]) -> dict:
    """聚合一个成交列表：gross（实现盈亏）、fee、net。"""
    gross = 0.0
    fee = 0.0
    by_sym: dict[str, dict] = {}
    for t in rows:
        sym = t.get("_symbol") or t.get("symbol") or "?"
        # ccxt 字段：
        # realizedPnl：USDT-M 成交上的已实现盈亏（来自 info）
        # fee.cost：手续费
        info = t.get("info") or {}
        rpnl = float(t.get("realizedPnl") or info.get("realizedPnl") or 0.0)
        fee_obj = t.get("fee") or {}
        fcost = float(fee_obj.get("cost") or 0.0)
        gross += rpnl
        fee += fcost
        d = by_sym.setdefault(sym, {"count": 0, "gross": 0.0, "fee": 0.0})
        d["count"] += 1
        d["gross"] += rpnl
        d["fee"] += fcost
    return {
        "count": len(rows),
        "gross": round(gross, 6),
        "fee": round(fee, 6),
        "net": round(gross - fee, 6),
        "by_symbol": {k: {**v, "net": round(v["gross"] - v["fee"], 6)} for k, v in by_sym.items()},
    }


async def main():
    start_ms = today_sha0_ms()
    start_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ms / 1000))
    now_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== 对账窗口 ===")
    print(f"本地 (CST) 今日 0:00 = {start_iso}  (epoch_ms={start_ms})")
    print(f"现在 (CST)         = {now_iso}")
    print()

    # 配置中的 symbols
    settings, _ = load_config(
        os.environ.get("BINANCE_CONFIG", "config.yaml"),
        os.environ.get("BINANCE_ENV", ".env"),
    )
    db_path = settings.storage.db_path or settings.storage.resolve_db_path(settings.mode)
    symbols = list(settings.symbols)
    print(f"mode={settings.mode}  db={db_path}  symbols={symbols}")
    print()

    # 1) 拉交易所成交
    print("=== 1) 拉取交易所 fetch_my_trades (since=今日 0:00) ===")
    try:
        trades = await fetch_exchange(symbols, start_ms)
    except Exception as e:
        print(f"[ERR] 拉取失败: {type(e).__name__}: {e}")
        return
    exch = summarize(trades)
    print(f"  count = {exch['count']}")
    print(f"  gross = {exch['gross']:+.4f}  fee = {exch['fee']:+.4f}  net = {exch['net']:+.4f}")
    for sym, d in exch["by_symbol"].items():
        print(f"    {sym}: count={d['count']} gross={d['gross']:+.4f} fee={d['fee']:+.4f} net={d['net']:+.4f}")
    print()

    # 2) 本地 trades 同窗口聚合
    print(f"=== 2) 本地 trades 同窗口聚合 (closed_at_ms >= {start_ms}) ===")
    local = query_local(db_path, start_ms)
    rows = local["rows"]
    g = sum(r["realized_pnl"] or 0.0 for r in rows)
    n = sum(r["net_realized_pnl"] or 0.0 for r in rows)
    fee = sum((r["total_fee"] or 0.0) for r in rows)
    print(f"  closed trades count = {len(rows)}")
    print(f"  gross (realized_pnl) = {g:+.4f}")
    print(f"  fee  (total_fee)     = {fee:+.4f}")
    print(f"  net   (net_realized_pnl) = {n:+.4f}")
    for r in rows:
        print(f"    {r['closed_at_local']}  {r['symbol']:8s} {r['direction']:5s} "
              f"qty_opened={r['qty_opened']:.4f} entry={r['entry_price']} "
              f"gross={r['realized_pnl']:+.4f} fee={r['total_fee']:+.4f} net={r['net_realized_pnl']:+.4f}")
    print()

    # 3) balance_snapshots 最新一条 day_realized_pnl
    print("=== 3) balance_snapshots 最新一条 ===")
    snap = local["snapshot"]
    if snap:
        print(f"  ts_local={snap['ts_local']}  day_realized_pnl={snap['day_realized_pnl']:+.4f}  "
              f"drawdown_pct={snap['drawdown_pct']:+.4f}")
    else:
        print("  (empty)")
    print()

    # 4) trades 表里"今天"被打开/关闭的若干最新行（用于排查是否漏算）
    print("=== 4) trades 表里今天相关最新 30 条（opened_at_ms 或 closed_at_ms >= 今天 0:00）===")
    for r in local["all_today"]:
        print(f"  id={r['id']:5d} {r['symbol']:8s} status={r['status']:8s} "
              f"opened={r['opened_at_local']} closed={r['closed_at_local']} net={r['net_realized_pnl']}")
    print()

    # 5) 差值
    print("=== 5) 差值（交易所 - 本地）===")
    print(f"  gross_diff = {exch['gross'] - g:+.4f}")
    print(f"  net_diff   = {exch['net']   - n:+.4f}")
    print(f"  fee_diff   = {exch['fee']   - fee:+.4f}")
    print()
    if snap:
        print(f"  snapshot.day_realized_pnl (最近) = {snap['day_realized_pnl']:+.4f}")


if __name__ == "__main__":
    asyncio.run(main())
