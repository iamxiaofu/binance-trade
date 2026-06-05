"""持久化仓库：异步落库与查询；启动对账。

封装一个 async engine + sessionmaker，提供高层写入方法供 engine 调用。
所有写入各自开 session 并 commit，避免长事务；读多写少场景下足够。

启动对账（reconcile）：从交易所拉取当前持仓 → 落一份快照 → 回填 RuntimeState，
使重启后内存态与交易所一致。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.exchange.positions import normalize_position, normalize_symbol
from src.llm.schema import MarketContext, TradeDecision
from src.risk.manager import Verdict
from src.state.runtime import RuntimeState
from src.store.models import (
    Base,
    BalanceSnapshotRow,
    ControlCommandRow,
    DecisionRow,
    OpenOrderRow,
    OrderRow,
    PositionSnapshotRow,
    RejectRow,
    RuntimeSettingRow,
)


class Store:
    """SQLite(aiosqlite) 异步持久化。使用后 await close()。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        # 确保父目录存在
        p = Path(db_path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{db_path}"
        self._engine = create_async_engine(url, echo=False, future=True)
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def connect(self) -> None:
        """建表（幂等）。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("store connected: {}", self._db_path)

    async def close(self) -> None:
        await self._engine.dispose()

    async def _add(self, row: Any) -> None:
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()

    # ---------- 决策日志 ----------
    async def log_decision(
        self,
        *,
        symbol: str,
        decision: TradeDecision | None = None,
        ctx: MarketContext | None = None,
        skipped: bool = False,
        skip_reason: str = "",
        ref_price: float = 0.0,
    ) -> None:
        row = DecisionRow(
            symbol=symbol,
            skipped=skipped,
            skip_reason=skip_reason[:200],
            ref_price=ref_price,
        )
        if decision is not None:
            row.action = decision.action.value
            row.confidence = decision.confidence
            row.size_pct = decision.size_pct
            row.leverage = decision.leverage
            row.stop_loss_pct = decision.stop_loss_pct
            row.take_profit_pct = decision.take_profit_pct
            row.reason = decision.reason[:500]
        if ctx is not None:
            try:
                row.context_json = ctx.model_dump_json()
            except Exception:  # 落库失败不能影响主流程
                row.context_json = ""
        await self._add(row)

    # ---------- 拒单 ----------
    async def log_reject(self, *, symbol: str, verdict: Verdict, decision: TradeDecision | None) -> None:
        row = RejectRow(
            symbol=symbol,
            code=verdict.code.value if verdict.code else "",
            reason=verdict.reason[:300],
        )
        if decision is not None:
            row.action = decision.action.value
            row.leverage = decision.leverage
            row.size_pct = decision.size_pct
        await self._add(row)

    # ---------- 订单 ----------
    async def log_order(self, order: dict) -> None:
        """order 为 executor 产出的标准化 dict。"""
        row = OrderRow(
            symbol=order.get("symbol", ""),
            client_kind=order.get("kind", ""),
            side=order.get("side", ""),
            order_type=order.get("order_type", ""),
            qty=float(order.get("qty") or 0.0),
            price=float(order.get("price") or 0.0),
            notional=float(order.get("notional") or 0.0),
            dry_run=bool(order.get("dry_run", True)),
            status=order.get("status", ""),
            exchange_order_id=str(order.get("id") or ""),
        )
        try:
            row.raw_json = json.dumps(order.get("raw") or {}, default=str)[:8000]
        except Exception:
            row.raw_json = ""
        await self._add(row)

    async def mark_orders_status_by_exchange_ids(
        self,
        exchange_order_ids: list[str] | set[str],
        status: str,
    ) -> int:
        """按交易所订单 id 批量更新本地订单状态，返回更新行数。"""
        ids = [str(x) for x in exchange_order_ids if str(x)]
        if not ids:
            return 0
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(OrderRow).where(OrderRow.exchange_order_id.in_(ids))
                )
            ).scalars().all()
            for row in rows:
                row.status = status
            await session.commit()
            return len(rows)

    async def mark_symbol_conditions_not_live(
        self,
        symbol: str,
        live_exchange_order_ids: set[str],
        status: str = "canceled",
    ) -> int:
        """无持仓对账时，把交易所已不在 open 列表的本地条件单更新为终态。"""
        symbol = normalize_symbol(symbol)
        async with self._sessionmaker() as session:
            stmt = (
                select(OrderRow)
                .where(OrderRow.symbol == symbol)
                .where(OrderRow.client_kind.in_(("SL", "TP")))
                .where(OrderRow.dry_run.is_(False))
                .where(OrderRow.status.in_(("placed", "open")))
            )
            rows = (await session.execute(stmt)).scalars().all()
            changed = 0
            for row in rows:
                if row.exchange_order_id and row.exchange_order_id in live_exchange_order_ids:
                    continue
                row.status = status
                changed += 1
            await session.commit()
            return changed

    # ---------- 快照 ----------
    async def snapshot_positions(
        self,
        positions: list[dict],
        symbols: list[str] | None = None,
    ) -> None:
        async with self._sessionmaker() as session:
            by_symbol: dict[str, dict] = {}
            for raw in positions:
                pos = normalize_position(raw)
                if pos["symbol"]:
                    by_symbol[pos["symbol"]] = pos

            tracked = list(by_symbol)
            for sym in symbols or []:
                ns = normalize_symbol(sym)
                if ns and ns not in by_symbol:
                    tracked.append(ns)

            for sym in tracked:
                pos = by_symbol.get(sym) or normalize_position({}, symbol=sym)
                if pos["contracts"] == 0 and symbols is None:
                    continue
                session.add(
                    PositionSnapshotRow(
                        symbol=pos["symbol"],
                        side=pos["side"],
                        contracts=pos["contracts"],
                        entry_price=pos["entry_price"],
                        mark_price=pos["mark_price"],
                        leverage=pos["leverage"],
                        unrealized_pnl=pos["unrealized_pnl"],
                        notional=pos["notional"],
                    )
                )
            await session.commit()

    async def mark_condition_exit(
        self,
        *,
        symbol: str,
        triggered_kind: str,
        qty: float,
        price: float,
    ) -> None:
        """标记最近一组 SL/TP 条件单：触发的一侧成交，另一侧取消。

        交易所侧 SL/TP 触发后，本系统通过持仓消失检测到平仓；这里把前面挂出的
        保护条件单状态补齐，供前端区分「成功挂出」与「触发成交」。
        """
        if triggered_kind not in ("SL", "TP"):
            return
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(OrderRow)
                    .where(OrderRow.symbol == symbol)
                    .where(OrderRow.client_kind.in_(("SL", "TP")))
                    .where(OrderRow.dry_run.is_(False))
                    .where(OrderRow.status.in_(("placed", "open", "filled")))
                    .order_by(OrderRow.id.desc())
                    .limit(2)
                )
            ).scalars().all()
            for row in rows:
                try:
                    raw = json.loads(row.raw_json or "{}")
                    if not isinstance(raw, dict):
                        raw = {"raw": raw}
                except Exception:
                    raw = {}
                if row.client_kind == triggered_kind:
                    row.status = "filled"
                    if qty > 0:
                        row.qty = qty
                    raw["trigger_price"] = row.price
                    raw["filled_price"] = price
                    raw["filled_qty"] = qty
                    raw["condition_exit_kind"] = triggered_kind
                    try:
                        row.raw_json = json.dumps(raw, default=str)[:8000]
                    except Exception:
                        pass
                    if price > 0:
                        row.notional = abs((row.qty or 0.0) * price)
                    else:
                        row.notional = abs((row.qty or 0.0) * (row.price or 0.0))
                elif row.status in ("placed", "open", "filled"):
                    row.status = "canceled"
            await session.commit()

    async def latest_protection_templates(
        self,
        symbol: str,
        *,
        dry_run: bool | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return latest local SL/TP rows for a symbol as repair templates.

        条件单在交易所侧没有仓位 id 绑定；补单只能用同 symbol 最近一次本地记录的
        SL/TP 触发价作为模板，并在 engine 侧再做当前标记价风控校验。
        """
        symbol = normalize_symbol(symbol)
        async with self._sessionmaker() as session:
            stmt = (
                select(OrderRow)
                .where(OrderRow.symbol == symbol)
                .where(OrderRow.client_kind.in_(("SL", "TP")))
                .where(OrderRow.price > 0)
            )
            if dry_run is not None:
                stmt = stmt.where(OrderRow.dry_run.is_(dry_run))
            rows = (
                await session.execute(stmt.order_by(OrderRow.id.desc()).limit(20))
            ).scalars().all()
            out: dict[str, dict[str, Any]] = {}
            for row in rows:
                if row.client_kind in out:
                    continue
                out[row.client_kind] = {
                    "id": row.id,
                    "symbol": row.symbol,
                    "kind": row.client_kind,
                    "side": row.side,
                    "order_type": row.order_type,
                    "qty": row.qty,
                    "price": row.price,
                    "status": row.status,
                    "dry_run": row.dry_run,
                    "exchange_order_id": row.exchange_order_id,
                    "ts_ms": row.ts_ms,
                    "created_at": row.created_at,
                }
            return out

    async def latest_open_decision(self, symbol: str) -> dict[str, Any] | None:
        """Return latest OPEN_LONG/OPEN_SHORT decision for fallback trigger reconstruction."""
        symbol = normalize_symbol(symbol)
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(DecisionRow)
                    .where(DecisionRow.symbol == symbol)
                    .where(DecisionRow.skipped.is_(False))
                    .where(DecisionRow.action.in_(("OPEN_LONG", "OPEN_SHORT")))
                    .order_by(DecisionRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "symbol": row.symbol,
                "action": row.action,
                "stop_loss_pct": row.stop_loss_pct,
                "take_profit_pct": row.take_profit_pct,
                "ref_price": row.ref_price,
                "ts_ms": row.ts_ms,
                "created_at": row.created_at,
            }

    async def snapshot_balance(
        self,
        *,
        total_equity: float,
        available_margin: float,
        runtime: RuntimeState,
        quote_asset: str = "USDT",
    ) -> None:
        await self._add(
            BalanceSnapshotRow(
                quote_asset=quote_asset,
                total_equity=total_equity,
                available_margin=available_margin,
                day_realized_pnl=runtime.day_realized_pnl,
                drawdown_pct=runtime.drawdown_pct,
            )
        )

    # ---------- 运行时设置 ----------
    async def set_runtime_setting(self, key: str, value: str) -> None:
        """持久化一项运行时设置。"""
        await self.set_runtime_settings({key: value})

    async def set_runtime_settings(self, settings: dict[str, str]) -> None:
        """在同一事务内持久化多项运行时设置。"""
        import time as _t

        now = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime())
        async with self._sessionmaker() as session:
            for key, value in settings.items():
                row = await session.get(RuntimeSettingRow, key)
                if row is None:
                    session.add(RuntimeSettingRow(key=key, value=value, updated_at=now))
                else:
                    row.value = value
                    row.updated_at = now
            await session.commit()

    async def get_runtime_setting(self, key: str) -> str | None:
        """读取单项运行时设置。"""
        async with self._sessionmaker() as session:
            row = await session.get(RuntimeSettingRow, key)
            return row.value if row is not None else None

    async def runtime_settings(self) -> dict[str, str]:
        """读取全部运行时设置，供 Web 展示有效运行态。"""
        async with self._sessionmaker() as session:
            rows = (await session.execute(select(RuntimeSettingRow))).scalars().all()
            return {row.key: row.value for row in rows}

    # ---------- 未完成挂单 ----------
    async def snapshot_open_orders(self, orders: list[dict]) -> None:
        """落库一批未完成挂单（ccxt order dict）。"""
        def _as_bool(val: Any) -> bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "y")
            return bool(val)

        async with self._sessionmaker() as session:
            for o in orders:
                info = o.get("info") or {}
                stop = o.get("stopPrice") or o.get("triggerPrice") or info.get("stopPrice") or 0
                session.add(
                    OpenOrderRow(
                        symbol=(o.get("symbol") or "").replace("/USDT:USDT", "USDT"),
                        exchange_order_id=str(o.get("id") or ""),
                        order_type=str(o.get("type") or ""),
                        side=str(o.get("side") or ""),
                        qty=float(o.get("amount") or 0),
                        price=float(o.get("price") or 0) if o.get("price") else 0.0,
                        stop_price=float(stop or 0),
                        reduce_only=_as_bool(o.get("reduceOnly") or info.get("reduceOnly")),
                        status=str(o.get("status") or ""),
                        raw_json=json.dumps(info, default=str)[:8000],
                    )
                )
            await session.commit()

    # ---------- 启动对账 ----------
    async def reconcile(
        self,
        positions: list[dict],
        runtime: RuntimeState,
        open_orders: list[dict] | None = None,
        symbols: list[str] | None = None,
    ) -> None:
        """与交易所对账：恢复当前持仓与未完成挂单，回填 RuntimeState。"""
        runtime.positions = {}
        for p in positions:
            pos = normalize_position(p)
            if pos["contracts"] == 0:
                continue
            runtime.positions[pos["symbol"]] = p
        await self.snapshot_positions(positions, symbols=symbols)

        runtime.open_orders = {}
        orders = open_orders or []
        for o in orders:
            sym = normalize_symbol(o.get("symbol"))
            runtime.open_orders.setdefault(sym, []).append(o)
        if orders:
            await self.snapshot_open_orders(orders)
        logger.info(
            "reconciled {} open positions, {} open orders on startup",
            len(runtime.positions), len(orders),
        )

    # ---------- 控制命令队列 ----------
    async def enqueue_command(self, command: str, arg: str = "", source: str = "web") -> int:
        """入队一条控制命令（web 侧调用）。返回命令 id。"""
        row = ControlCommandRow(command=command, arg=arg, source=source, status="pending")
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()
            return row.id

    async def fetch_pending_commands(self) -> list[dict]:
        """取出所有 pending 命令（engine 快速轮询）。返回普通 dict 列表。"""
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(ControlCommandRow)
                    .where(ControlCommandRow.status == "pending")
                    .order_by(ControlCommandRow.id)
                )
            ).scalars().all()
            return [
                {"id": r.id, "command": r.command, "arg": r.arg, "source": r.source}
                for r in rows
            ]

    async def mark_command(self, cmd_id: int, status: str, result: str = "") -> None:
        """标记命令执行结果（done/failed）。"""
        import time as _t
        async with self._sessionmaker() as session:
            row = await session.get(ControlCommandRow, cmd_id)
            if row is None:
                return
            row.status = status
            row.result = result[:300]
            row.executed_at = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime())
            await session.commit()

    async def recent_commands(self, limit: int = 50) -> list[dict]:
        """最近的命令记录（web 展示用）。"""
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(ControlCommandRow).order_by(ControlCommandRow.id.desc()).limit(limit)
                )
            ).scalars().all()
            return [
                {"id": r.id, "created_at": r.created_at, "command": r.command, "arg": r.arg,
                 "source": r.source, "status": r.status, "result": r.result,
                 "executed_at": r.executed_at}
                for r in rows
            ]
