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
from sqlalchemy import text, select
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
    SymbolRow,
    TradeRow,
)


_ORDER_EXTENSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("trade_id", "INTEGER NOT NULL DEFAULT 0"),
    ("trade_role", "VARCHAR(24) NOT NULL DEFAULT ''"),
    ("leverage", "INTEGER NOT NULL DEFAULT 0"),
    ("margin", "FLOAT NOT NULL DEFAULT 0.0"),
    ("realized_pnl", "FLOAT NOT NULL DEFAULT 0.0"),
)
_SYMBOL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("enabled", "BOOLEAN NOT NULL DEFAULT 0"),
    ("status", "VARCHAR(16) NOT NULL DEFAULT 'active'"),
    ("sync_status", "VARCHAR(32) NOT NULL DEFAULT 'new'"),
    ("needs_review", "BOOLEAN NOT NULL DEFAULT 0"),
    ("source", "VARCHAR(16) NOT NULL DEFAULT 'web'"),
    ("min_qty", "FLOAT NOT NULL DEFAULT 0.0"),
    ("min_notional", "FLOAT NOT NULL DEFAULT 0.0"),
    ("tick_size", "FLOAT NOT NULL DEFAULT 0.0"),
    ("step_size", "FLOAT NOT NULL DEFAULT 0.0"),
    ("raw_filters_json", "TEXT NOT NULL DEFAULT ''"),
    ("exchange_state_json", "TEXT NOT NULL DEFAULT ''"),
    ("added_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("updated_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("last_filter_sync_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
)

_FILLED_ORDER_STATUSES = {"filled", "partial"}
_TRIGGERED_CONDITION_STATUSES = {"filled", "partial"}
_OPEN_TRADE_STATUSES = {"open", "partial"}


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _now_iso_utc() -> str:
    import time as _t

    return _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime())


def _direction_from_open_side(side: str) -> str:
    side = (side or "").lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return ""


def _direction_from_close_side(side: str) -> str:
    side = (side or "").lower()
    if side == "sell":
        return "long"
    if side == "buy":
        return "short"
    return ""


def _trade_role(kind: str) -> str:
    return {
        "OPEN": "ENTRY",
        "CLOSE": "EXIT",
        "SL": "PROTECTION_SL",
        "TP": "PROTECTION_TP",
    }.get(kind, "")


def _realized_pnl(*, direction: str, entry_price: float, exit_price: float, qty: float) -> float:
    if direction not in ("long", "short") or entry_price <= 0 or exit_price <= 0 or qty <= 0:
        return 0.0
    sign = 1.0 if direction == "long" else -1.0
    return (exit_price - entry_price) * qty * sign


def _margin(notional: float, leverage: int) -> float:
    return abs(notional) / leverage if leverage > 0 else 0.0


def _pnl_pct(pnl: float, margin: float) -> float:
    return (pnl / margin) * 100.0 if margin > 0 else 0.0


def _raw_number(raw_json: str, key: str) -> float:
    try:
        raw = json.loads(raw_json or "{}")
    except Exception:
        return 0.0
    if not isinstance(raw, dict):
        return 0.0
    return _safe_float(raw.get(key))


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
            await self._upgrade_schema(conn)
        await self.backfill_trades()
        logger.info("store connected: {}", self._db_path)

    async def _upgrade_schema(self, conn) -> None:
        """SQLite 轻量迁移：create_all 不会给既有表自动补列。"""
        existing = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(orders)"))).fetchall()
        }
        for name, ddl in _ORDER_EXTENSION_COLUMNS:
            if name not in existing:
                await conn.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {ddl}"))
        symbol_existing = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(symbols)"))).fetchall()
        }
        for name, ddl in _SYMBOL_COLUMNS:
            if symbol_existing and name not in symbol_existing:
                await conn.execute(text(f"ALTER TABLE symbols ADD COLUMN {name} {ddl}"))

    async def close(self) -> None:
        await self._engine.dispose()

    async def _add(self, row: Any) -> None:
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()

    # ---------- 币种注册表 ----------
    @staticmethod
    def _symbol_row_dict(row: SymbolRow) -> dict[str, Any]:
        return {
            "symbol": row.symbol,
            "enabled": bool(row.enabled),
            "status": row.status,
            "sync_status": row.sync_status,
            "needs_review": bool(row.needs_review),
            "source": row.source,
            "min_qty": row.min_qty,
            "min_notional": row.min_notional,
            "tick_size": row.tick_size,
            "step_size": row.step_size,
            "raw_filters_json": row.raw_filters_json,
            "exchange_state_json": row.exchange_state_json,
            "added_at": row.added_at,
            "updated_at": row.updated_at,
            "last_filter_sync_at": row.last_filter_sync_at,
        }

    async def sync_config_symbols(self, symbols: list[str]) -> None:
        """把 config.yaml 中的静态币种 seed 到注册表，保留既有运行态开关。"""
        now = _now_iso_utc()
        normalized = []
        seen: set[str] = set()
        for raw in symbols:
            symbol = normalize_symbol(raw)
            if symbol and symbol not in seen:
                normalized.append(symbol)
                seen.add(symbol)

        async with self._sessionmaker() as session:
            for symbol in normalized:
                row = await session.get(SymbolRow, symbol)
                runtime = await session.get(RuntimeSettingRow, f"symbol.enabled.{symbol}")
                enabled = (
                    _parse_bool(runtime.value, True) if runtime is not None else True
                )
                if row is None:
                    session.add(
                        SymbolRow(
                            symbol=symbol,
                            enabled=enabled,
                            status="active",
                            sync_status="config_seed",
                            needs_review=False,
                            source="config",
                            added_at=now,
                            updated_at=now,
                        )
                    )
                    continue
                row.status = "active" if row.status != "archived" else row.status
                row.source = "config" if row.source in ("", "web") else row.source
                if runtime is not None:
                    row.enabled = enabled
                row.updated_at = now
            await session.commit()

    async def list_symbols(self, include_archived: bool = False) -> list[dict[str, Any]]:
        async with self._sessionmaker() as session:
            stmt = select(SymbolRow)
            if not include_archived:
                stmt = stmt.where(SymbolRow.status != "archived")
            rows = (
                await session.execute(stmt.order_by(SymbolRow.added_at, SymbolRow.symbol))
            ).scalars().all()
            return [self._symbol_row_dict(row) for row in rows]

    async def active_symbols(self) -> list[str]:
        rows = await self.list_symbols(include_archived=False)
        return [row["symbol"] for row in rows if row["status"] == "active"]

    async def enabled_symbols(self) -> list[str]:
        rows = await self.list_symbols(include_archived=False)
        return [
            row["symbol"]
            for row in rows
            if row["status"] == "active" and row["enabled"] and not row["needs_review"]
        ]

    async def get_symbol(self, symbol: str) -> dict[str, Any] | None:
        symbol = normalize_symbol(symbol)
        async with self._sessionmaker() as session:
            row = await session.get(SymbolRow, symbol)
            return self._symbol_row_dict(row) if row is not None else None

    async def upsert_symbol_from_exchange(
        self,
        *,
        symbol: str,
        filters: Any,
        exchange_state: dict[str, Any],
        source: str = "web",
        enabled: bool = False,
        sync_status: str = "confirmed_flat",
        needs_review: bool = False,
    ) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        if not symbol:
            raise ValueError("symbol is required")
        now = _now_iso_utc()

        raw_filters = {
            "tick_size": str(getattr(filters, "tick_size", "")),
            "step_size": str(getattr(filters, "step_size", "")),
            "min_qty": str(getattr(filters, "min_qty", "")),
            "min_notional": str(getattr(filters, "min_notional", "")),
        }
        state_json = json.dumps(exchange_state or {}, ensure_ascii=False, default=str)[:12000]
        filters_json = json.dumps(raw_filters, ensure_ascii=False, default=str)
        async with self._sessionmaker() as session:
            row = await session.get(SymbolRow, symbol)
            if row is None:
                row = SymbolRow(symbol=symbol, added_at=now)
                session.add(row)
            row.enabled = enabled
            row.status = "active"
            row.sync_status = sync_status
            row.needs_review = needs_review
            row.source = source[:16]
            row.min_qty = float(getattr(filters, "min_qty", 0) or 0)
            row.min_notional = float(getattr(filters, "min_notional", 0) or 0)
            row.tick_size = float(getattr(filters, "tick_size", 0) or 0)
            row.step_size = float(getattr(filters, "step_size", 0) or 0)
            row.raw_filters_json = filters_json
            row.exchange_state_json = state_json
            row.updated_at = now
            row.last_filter_sync_at = now
            runtime = await session.get(RuntimeSettingRow, f"symbol.enabled.{symbol}")
            if runtime is None:
                session.add(
                    RuntimeSettingRow(
                        key=f"symbol.enabled.{symbol}",
                        value=str(enabled).lower(),
                        updated_at=now,
                    )
                )
            else:
                runtime.value = str(enabled).lower()
                runtime.updated_at = now
            await session.commit()
            return self._symbol_row_dict(row)

    async def update_symbol_filters(self, symbol: str, filters: Any) -> None:
        """只更新交易所 filters，不改变启停状态和复核状态。"""
        symbol = normalize_symbol(symbol)
        now = _now_iso_utc()
        raw_filters = {
            "tick_size": str(getattr(filters, "tick_size", "")),
            "step_size": str(getattr(filters, "step_size", "")),
            "min_qty": str(getattr(filters, "min_qty", "")),
            "min_notional": str(getattr(filters, "min_notional", "")),
        }
        async with self._sessionmaker() as session:
            row = await session.get(SymbolRow, symbol)
            if row is None:
                return
            row.min_qty = float(getattr(filters, "min_qty", 0) or 0)
            row.min_notional = float(getattr(filters, "min_notional", 0) or 0)
            row.tick_size = float(getattr(filters, "tick_size", 0) or 0)
            row.step_size = float(getattr(filters, "step_size", 0) or 0)
            row.raw_filters_json = json.dumps(raw_filters, ensure_ascii=False, default=str)
            row.updated_at = now
            row.last_filter_sync_at = now
            await session.commit()

    async def set_symbol_enabled(self, symbol: str, enabled: bool) -> None:
        symbol = normalize_symbol(symbol)
        now = _now_iso_utc()
        async with self._sessionmaker() as session:
            row = await session.get(SymbolRow, symbol)
            if row is None or row.status == "archived":
                raise ValueError(f"symbol not registered: {symbol}")
            row.enabled = enabled
            row.updated_at = now
            runtime = await session.get(RuntimeSettingRow, f"symbol.enabled.{symbol}")
            if runtime is None:
                session.add(
                    RuntimeSettingRow(
                        key=f"symbol.enabled.{symbol}",
                        value=str(enabled).lower(),
                        updated_at=now,
                    )
                )
            else:
                runtime.value = str(enabled).lower()
                runtime.updated_at = now
            await session.commit()

    async def backfill_trades(self) -> int:
        """把没有 trade_id 的历史订单按仓位生命周期归组，幂等执行。"""
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(OrderRow)
                    .where(OrderRow.trade_id == 0)
                    .where(OrderRow.client_kind.in_(("OPEN", "SL", "TP", "CLOSE")))
                    .order_by(OrderRow.ts_ms, OrderRow.id)
                )
            ).scalars().all()
            changed = 0
            for row in rows:
                before = row.trade_id
                await self._attach_order_to_trade(
                    session, row, source="backfill", confidence="inferred"
                )
                if row.trade_id != before:
                    changed += 1
            await session.commit()
            if changed:
                logger.info("backfilled {} historical orders into trades", changed)
            return changed

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
    async def log_order(self, order: dict) -> dict[str, int]:
        """order 为 executor 产出的标准化 dict。"""
        async with self._sessionmaker() as session:
            row = self._order_row_from_dict(order)
            session.add(row)
            await session.flush()
            await self._attach_order_to_trade(session, row, source="live", confidence="exact")
            await session.commit()
            return {"order_id": row.id, "trade_id": row.trade_id}

    def _order_row_from_dict(self, order: dict) -> OrderRow:
        row = OrderRow(
            symbol=order.get("symbol", ""),
            client_kind=order.get("kind", ""),
            side=order.get("side", ""),
            order_type=order.get("order_type", ""),
            qty=_safe_float(order.get("qty")),
            price=_safe_float(order.get("price")),
            notional=_safe_float(order.get("notional")),
            dry_run=bool(order.get("dry_run", False)),
            status=order.get("status", ""),
            exchange_order_id=str(order.get("id") or ""),
            leverage=_safe_int(order.get("leverage")),
            margin=_safe_float(order.get("margin")),
            realized_pnl=_safe_float(order.get("realized_pnl")),
        )
        trade_id = _safe_int(order.get("trade_id"))
        if trade_id > 0:
            row.trade_id = trade_id
        role = str(order.get("trade_role") or _trade_role(row.client_kind))
        row.trade_role = role[:24]
        try:
            row.raw_json = json.dumps(order.get("raw") or {}, default=str)[:8000]
        except Exception:
            row.raw_json = ""
        return row

    async def _attach_order_to_trade(
        self,
        session: AsyncSession,
        row: OrderRow,
        *,
        source: str,
        confidence: str,
    ) -> None:
        kind = row.client_kind
        status = str(row.status or "")
        if kind == "OPEN":
            if status in _FILLED_ORDER_STATUSES:
                trade = await self._create_trade_from_open(
                    session, row, source=source, confidence=confidence
                )
                row.trade_id = trade.id
                row.trade_role = "ENTRY"
                row.leverage = trade.leverage
                row.margin = trade.entry_margin
            return

        if kind in ("SL", "TP"):
            trade = await self._find_open_trade_for_order(session, row)
            if trade is None:
                trade = await self._find_recent_trade_for_condition(session, row)
            if trade is None:
                return
            row.trade_id = trade.id
            row.trade_role = _trade_role(kind)
            row.leverage = trade.leverage
            row.margin = _margin(row.notional, trade.leverage)
            if status in _TRIGGERED_CONDITION_STATUSES and trade.status != "closed":
                self._close_trade_with_order(trade, row, exit_reason=kind)
            return

        if kind == "CLOSE":
            trade = await self._find_open_trade_for_order(session, row)
            if trade is None:
                return
            row.trade_id = trade.id
            row.trade_role = "EXIT"
            row.leverage = trade.leverage
            row.margin = _margin(row.notional, trade.leverage)
            if status in _FILLED_ORDER_STATUSES:
                self._close_trade_with_order(trade, row, exit_reason="CLOSE")

    async def _create_trade_from_open(
        self,
        session: AsyncSession,
        row: OrderRow,
        *,
        source: str,
        confidence: str,
    ) -> TradeRow:
        direction = _direction_from_open_side(row.side)
        leverage = row.leverage or await self._infer_leverage(session, row)
        entry_margin = row.margin or _margin(row.notional, leverage)
        trade = TradeRow(
            ts_ms=row.ts_ms,
            created_at=row.created_at,
            symbol=row.symbol,
            direction=direction,
            status="open",
            dry_run=row.dry_run,
            opened_at_ms=row.ts_ms,
            opened_at=row.created_at,
            entry_order_id=row.id,
            entry_price=row.price,
            qty_opened=row.qty,
            leverage=leverage,
            entry_notional=row.notional,
            entry_margin=entry_margin,
            source=source,
            confidence=confidence,
        )
        session.add(trade)
        await session.flush()
        return trade

    async def _find_open_trade_for_order(self, session: AsyncSession, row: OrderRow) -> TradeRow | None:
        if row.trade_id:
            trade = await session.get(TradeRow, row.trade_id)
            if trade is not None:
                return trade

        direction = _direction_from_close_side(row.side)
        stmt = (
            select(TradeRow)
            .where(TradeRow.symbol == row.symbol)
            .where(TradeRow.status.in_(tuple(_OPEN_TRADE_STATUSES)))
            .where(TradeRow.dry_run.is_(row.dry_run))
            .where(TradeRow.opened_at_ms <= row.ts_ms)
        )
        if direction:
            stmt = stmt.where(TradeRow.direction == direction)
        rows = (
            await session.execute(
                stmt.order_by(TradeRow.opened_at_ms.desc(), TradeRow.id.desc())
            )
        ).scalars().all()
        for trade in rows:
            if self._qty_matches(trade.qty_opened, row.qty):
                return trade
        return rows[0] if rows else None

    async def _find_recent_trade_for_condition(
        self,
        session: AsyncSession,
        row: OrderRow,
    ) -> TradeRow | None:
        direction = _direction_from_close_side(row.side)
        stmt = (
            select(TradeRow)
            .where(TradeRow.symbol == row.symbol)
            .where(TradeRow.dry_run.is_(row.dry_run))
            .where(TradeRow.opened_at_ms <= row.ts_ms)
        )
        if direction:
            stmt = stmt.where(TradeRow.direction == direction)
        rows = (
            await session.execute(
                stmt.order_by(TradeRow.opened_at_ms.desc(), TradeRow.id.desc()).limit(20)
            )
        ).scalars().all()
        for trade in rows:
            if self._qty_matches(trade.qty_opened, row.qty):
                return trade
        return None

    @staticmethod
    def _qty_matches(base_qty: float, row_qty: float) -> bool:
        if base_qty <= 0 or row_qty <= 0:
            return False
        return abs(base_qty - row_qty) <= max(abs(base_qty) * 1e-6, 1e-12)

    async def _infer_leverage(self, session: AsyncSession, row: OrderRow) -> int:
        action = "OPEN_LONG" if row.side == "buy" else "OPEN_SHORT" if row.side == "sell" else ""
        if not action:
            return 0
        stmt = (
            select(DecisionRow.leverage)
            .where(DecisionRow.symbol == row.symbol)
            .where(DecisionRow.skipped.is_(False))
            .where(DecisionRow.action == action)
            .where(DecisionRow.ts_ms <= row.ts_ms)
            .order_by(DecisionRow.ts_ms.desc(), DecisionRow.id.desc())
            .limit(1)
        )
        leverage = (await session.execute(stmt)).scalar_one_or_none()
        return _safe_int(leverage)

    def _close_trade_with_order(self, trade: TradeRow, row: OrderRow, *, exit_reason: str) -> None:
        qty = row.qty if row.qty > 0 else trade.qty_opened
        exit_price = _raw_number(row.raw_json, "filled_price") or row.price
        pnl = row.realized_pnl or _realized_pnl(
            direction=trade.direction,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            qty=qty,
        )
        margin = trade.entry_margin or _margin(trade.entry_notional, trade.leverage)
        trade.status = "closed"
        trade.closed_at_ms = row.ts_ms
        trade.closed_at = row.created_at
        trade.exit_order_id = row.id
        trade.exit_price = exit_price
        trade.qty_closed = qty
        trade.exit_notional = abs(qty * exit_price) if exit_price > 0 else row.notional
        trade.realized_pnl = pnl
        trade.pnl_pct_on_margin = _pnl_pct(pnl, margin)
        trade.exit_reason = exit_reason
        row.realized_pnl = pnl

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
                await self._refresh_trade_for_existing_order(session, row)
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
                await self._refresh_trade_for_existing_order(session, row)
                changed += 1
            await session.commit()
            return changed

    async def _refresh_trade_for_existing_order(self, session: AsyncSession, row: OrderRow) -> None:
        if row.client_kind not in ("SL", "TP", "CLOSE"):
            return
        if row.trade_id == 0:
            await self._attach_order_to_trade(session, row, source="backfill", confidence="inferred")
            return
        if row.client_kind in ("SL", "TP") and str(row.status or "") not in _TRIGGERED_CONDITION_STATUSES:
            return
        if row.client_kind == "CLOSE" and str(row.status or "") not in _FILLED_ORDER_STATUSES:
            return
        trade = await session.get(TradeRow, row.trade_id)
        if trade is None or trade.status == "closed":
            return
        self._close_trade_with_order(
            trade,
            row,
            exit_reason=row.client_kind if row.client_kind in ("SL", "TP") else "CLOSE",
        )

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
                    await self._refresh_trade_for_existing_order(session, row)
                elif row.status in ("placed", "open", "filled"):
                    row.status = "canceled"
                    await self._refresh_trade_for_existing_order(session, row)
            await session.commit()

    async def latest_protection_templates(
        self,
        symbol: str,
        *,
        dry_run: bool | None = False,
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
