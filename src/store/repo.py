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

    # ---------- 快照 ----------
    async def snapshot_positions(self, positions: list[dict]) -> None:
        async with self._sessionmaker() as session:
            for p in positions:
                contracts = float(p.get("contracts") or 0)
                if contracts == 0:
                    continue
                mark = float(p.get("markPrice") or p.get("entryPrice") or 0)
                session.add(
                    PositionSnapshotRow(
                        symbol=(p.get("symbol") or "").replace("/USDT:USDT", "USDT"),
                        side=(p.get("side") or ""),
                        contracts=contracts,
                        entry_price=float(p.get("entryPrice") or 0),
                        mark_price=mark,
                        leverage=int(float(p.get("leverage") or 0)),
                        unrealized_pnl=float(p.get("unrealizedPnl") or 0),
                        notional=abs(contracts) * mark,
                    )
                )
            await session.commit()

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

    # ---------- 未完成挂单 ----------
    async def snapshot_open_orders(self, orders: list[dict]) -> None:
        """落库一批未完成挂单（ccxt order dict）。"""
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
                        reduce_only=bool(o.get("reduceOnly") or info.get("reduceOnly")),
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
    ) -> None:
        """与交易所对账：恢复当前持仓与未完成挂单，回填 RuntimeState。"""
        runtime.positions = {}
        for p in positions:
            if float(p.get("contracts") or 0) == 0:
                continue
            sym = (p.get("symbol") or "").replace("/USDT:USDT", "USDT")
            runtime.positions[sym] = p
        await self.snapshot_positions(positions)

        runtime.open_orders = {}
        orders = open_orders or []
        for o in orders:
            sym = (o.get("symbol") or "").replace("/USDT:USDT", "USDT")
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
        """取出所有 pending 命令（engine 每周期调用）。返回普通 dict 列表。"""
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
