"""持久化仓库：异步落库与查询；启动对账。

封装一个 async engine + sessionmaker，提供高层写入方法供 engine 调用。
所有写入各自开 session 并 commit，避免长事务；读多写少场景下足够。

启动对账（reconcile）：从交易所拉取当前持仓 → 落一份快照 → 回填 RuntimeState，
使重启后内存态与交易所一致。
"""
from __future__ import annotations
import time  # noqa: F811  # for day_realized_pnl_by_local_day
import time as _t

import json
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import text, select, or_, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.exchange.positions import normalize_position, normalize_symbol
from src.llm.schema import DECISION_REASON_MAX_LENGTH, MarketContext, TradeDecision
from src.risk.manager import Verdict
from src.state.runtime import RuntimeState
from src.store.models import (
    Base,
    BalanceSnapshotRow,
    ControlCommandRow,
    DecisionRow,
    OpenOrderRow,
    OrderRow,
    PositionClaimRow,
    PositionSnapshotRow,
    RejectRow,
    LLMProfileRow,
    RuntimeSettingRow,
    SymbolRow,
    TradeRow,
    ExchangeEventRow,
    ExchangeStateDriftRow,
    ExchangeStreamSessionRow,
    LiveBalanceRow,
    LivePositionRow,
    LiveOrderRow,
)
from src.exchange.events import ExchangeEvent
from src.exchange.orders import normalize_condition_order, normalize_open_order


_ORDER_EXTENSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("trade_id", "INTEGER NOT NULL DEFAULT 0"),
    ("trade_role", "VARCHAR(24) NOT NULL DEFAULT ''"),
    ("leverage", "INTEGER NOT NULL DEFAULT 0"),
    ("margin", "FLOAT NOT NULL DEFAULT 0.0"),
    ("realized_pnl", "FLOAT NOT NULL DEFAULT 0.0"),
    ("execution_mode", "VARCHAR(24) NOT NULL DEFAULT ''"),
    ("time_in_force", "VARCHAR(12) NOT NULL DEFAULT ''"),
    ("requested_qty", "FLOAT NOT NULL DEFAULT 0.0"),
    ("filled_qty", "FLOAT NOT NULL DEFAULT 0.0"),
    ("remaining_qty", "FLOAT NOT NULL DEFAULT 0.0"),
    ("requested_price", "FLOAT NOT NULL DEFAULT 0.0"),
    ("limit_price", "FLOAT NOT NULL DEFAULT 0.0"),
    ("avg_price", "FLOAT NOT NULL DEFAULT 0.0"),
    ("liquidity", "VARCHAR(12) NOT NULL DEFAULT ''"),
    ("fee", "FLOAT NOT NULL DEFAULT 0.0"),
    ("fee_asset", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("client_order_id", "VARCHAR(64) NOT NULL DEFAULT ''"),
)
_TRADE_EXTENSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entry_fee", "FLOAT NOT NULL DEFAULT 0.0"),
    ("exit_fee", "FLOAT NOT NULL DEFAULT 0.0"),
    ("total_fee", "FLOAT NOT NULL DEFAULT 0.0"),
    ("gross_realized_pnl", "FLOAT NOT NULL DEFAULT 0.0"),
    ("net_realized_pnl", "FLOAT NOT NULL DEFAULT 0.0"),
    ("net_pnl_pct_on_margin", "FLOAT NOT NULL DEFAULT 0.0"),
    ("entry_liquidity", "VARCHAR(12) NOT NULL DEFAULT ''"),
    ("exit_liquidity", "VARCHAR(12) NOT NULL DEFAULT ''"),
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
    ("disabled_reason_code", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("disabled_reason", "TEXT NOT NULL DEFAULT ''"),
    ("disabled_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("disabled_source", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("disabled_action", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("last_enabled_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("added_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("updated_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("last_filter_sync_at", "VARCHAR(32) NOT NULL DEFAULT ''"),
)
_DECISION_EXTENSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("llm_prompt", "TEXT NOT NULL DEFAULT ''"),
    ("llm_request_json", "TEXT NOT NULL DEFAULT ''"),
    ("llm_response_json", "TEXT NOT NULL DEFAULT ''"),
    ("feature_snapshot_json", "TEXT NOT NULL DEFAULT ''"),
    ("llm_latency_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("llm_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("llm_status", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("llm_error", "VARCHAR(200) NOT NULL DEFAULT ''"),
)
_LLM_PROFILE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("api_key", "TEXT NOT NULL DEFAULT ''"),
    ("priority", "INTEGER NOT NULL DEFAULT 100"),
    ("fallback_enabled", "INTEGER NOT NULL DEFAULT 0"),
)
_SQLITE_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_decisions_ts_id ON decisions(ts_ms DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_decisions_symbol_ts_id ON decisions(symbol, ts_ms DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_trades_opened_id ON trades(opened_at_ms DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_trades_symbol_opened_id ON trades(symbol, opened_at_ms DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_trades_status_opened_id ON trades(status, opened_at_ms DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_orders_trade_ts_id ON orders(trade_id, ts_ms ASC, id ASC)",
)

_FILLED_ORDER_STATUSES = {"filled", "partial"}
_TRIGGERED_CONDITION_STATUSES = {"filled", "partial", "triggered"}
_OPEN_TRADE_STATUSES = {"open", "partial"}
_ACTIVE_CLAIM_STATUSES = {"opening", "submitted", "protecting"}
_RECENT_ENTRY_CLAIM_STATUSES = _ACTIVE_CLAIM_STATUSES | {
    "filled", "partial", "error", "canceled", "rejected", "expired",
}


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


def _weighted_exit_price_from_trades(
    trades: list[dict[str, Any]],
    *,
    direction: str,
    since_ms: int,
    target_qty: float,
) -> float:
    """从 ccxt 形态的成交列表中，挑出 trade 开仓之后、平仓方向、数量累计
    不低于 target_qty 的部分做加权均价。无法确定时返回 0.0。

    ``trades`` 既可以是 ``fetch_my_trades`` 全量结果，也可以是已经按时间窗口
    过滤后的子集；函数内部再按 ``timestamp >= since_ms`` 与反向 side 二次过滤。
    """
    if target_qty <= 0 or direction not in ("long", "short"):
        return 0.0
    close_side = "sell" if direction == "long" else "buy"
    pool: list[tuple[int, float, float]] = []  # (timestamp, price, amount)
    for t in trades or []:
        try:
            ts = int(t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < since_ms:
            continue
        side = (t.get("side") or "").lower()
        if side != close_side:
            continue
        info = t.get("info") or {}
        info_side = (info.get("side") or "").upper()
        # ccxt 在 USDT-M 上对 reduce-only 平仓通常 side=BUY/SELL，但有些
        # 限频时只放在 info.side 里；二者任一为 close_side 即视为平仓。
        if info_side and info_side.lower() != close_side:
            continue
        try:
            price = float(t.get("price") or 0.0)
            amount = float(t.get("amount") or 0.0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or amount <= 0:
            continue
        pool.append((ts, price, amount))
    if not pool:
        return 0.0
    pool.sort(key=lambda x: x[0])
    remaining = target_qty
    sum_notional = 0.0
    sum_qty = 0.0
    for _ts, price, amount in pool:
        if remaining <= 0:
            break
        take = min(amount, remaining)
        sum_notional += price * take
        sum_qty += take
        remaining -= take
    if sum_qty <= 0:
        return 0.0
    # 如果成交累计量仍不足 target_qty，至少给一个保守的"已知成交量均价"。
    return sum_notional / sum_qty


def _sum_fee_from_trades(
    trades: list[dict[str, Any]],
    *,
    direction: str,
    since_ms: int,
    target_qty: float,
) -> float:
    """从 ccxt myTrades 列表中累计与 ``_weighted_exit_price_from_trades`` 命中的
    同一窗口的手续费（按 cost 直接相加，不分买卖）。"""
    if target_qty <= 0 or direction not in ("long", "short"):
        return 0.0
    close_side = "sell" if direction == "long" else "buy"
    pool: list[tuple[int, float, float]] = []
    for t in trades or []:
        try:
            ts = int(t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < since_ms:
            continue
        side = (t.get("side") or "").lower()
        if side != close_side:
            continue
        info = t.get("info") or {}
        info_side = (info.get("side") or "").upper()
        if info_side and info_side.lower() != close_side:
            continue
        try:
            amount = float(t.get("amount") or 0.0)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        pool.append((ts, amount, 0.0))
    if not pool:
        return 0.0
    pool.sort(key=lambda x: x[0])
    remaining = target_qty
    total = 0.0
    for t, (ts, amount, _) in zip(trades, pool):
        if remaining <= 0:
            break
        take = min(amount, remaining)
        fee_obj = t.get("fee") or {}
        try:
            cost = float(fee_obj.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        # cost 已经按 amount 给出，按 take / amount 比例分摊
        if amount > 0:
            total += cost * (take / amount)
        remaining -= take
    return total


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
        try:
            Path(self._db_path).chmod(0o600)
        except OSError as e:
            logger.warning("failed to restrict sqlite permissions {}: {}", self._db_path, e)
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
        trade_existing = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(trades)"))).fetchall()
        }
        for name, ddl in _TRADE_EXTENSION_COLUMNS:
            if trade_existing and name not in trade_existing:
                await conn.execute(text(f"ALTER TABLE trades ADD COLUMN {name} {ddl}"))
        symbol_existing = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(symbols)"))).fetchall()
        }
        for name, ddl in _SYMBOL_COLUMNS:
            if symbol_existing and name not in symbol_existing:
                await conn.execute(text(f"ALTER TABLE symbols ADD COLUMN {name} {ddl}"))
        decision_existing = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(decisions)"))).fetchall()
        }
        for name, ddl in _DECISION_EXTENSION_COLUMNS:
            if decision_existing and name not in decision_existing:
                await conn.execute(text(f"ALTER TABLE decisions ADD COLUMN {name} {ddl}"))
        llm_profile_existing = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(llm_profiles)"))).fetchall()
        }
        for name, ddl in _LLM_PROFILE_COLUMNS:
            if llm_profile_existing and name not in llm_profile_existing:
                await conn.execute(text(f"ALTER TABLE llm_profiles ADD COLUMN {name} {ddl}"))
        for ddl in _SQLITE_INDEX_DDL:
            await conn.execute(text(ddl))

    async def close(self) -> None:
        await self._engine.dispose()

    async def _add(self, row: Any) -> None:
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()

    # ---------- 私有流事件与当前账户投影 ----------
    async def record_exchange_event(self, event: ExchangeEvent) -> bool:
        row = ExchangeEventRow(
            event_key=event.event_key,
            session_id=event.session_id,
            source=event.source,
            event_type=event.event_type,
            event_time_ms=event.event_time_ms,
            transaction_time_ms=event.transaction_time_ms,
            received_at_ms=event.received_at_ms,
            raw_json=json.dumps(event.payload, default=str)[:100000],
        )
        async with self._sessionmaker() as session:
            session.add(row)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def pending_exchange_events(self, limit: int = 10000) -> list[ExchangeEvent]:
        async with self._sessionmaker() as session:
            rows = (await session.execute(
                select(ExchangeEventRow)
                .where(ExchangeEventRow.status == "received")
                .order_by(ExchangeEventRow.id)
                .limit(limit)
            )).scalars().all()
        events: list[ExchangeEvent] = []
        for row in rows:
            try:
                payload = json.loads(row.raw_json or "{}")
            except json.JSONDecodeError:
                continue
            events.append(ExchangeEvent(
                event_type=row.event_type,
                payload=payload,
                event_time_ms=row.event_time_ms,
                transaction_time_ms=row.transaction_time_ms,
                source=row.source,
                session_id=row.session_id,
                received_at_ms=row.received_at_ms,
                event_key=row.event_key,
            ))
        return events

    async def exchange_event_stats(self) -> dict[str, int]:
        async with self._sessionmaker() as session:
            row = (await session.execute(text(
                "SELECT COALESCE(MAX(received_at_ms), 0), "
                "SUM(CASE WHEN status='received' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) "
                "FROM exchange_events"
            ))).first()
        return {
            "last_event_at_ms": int(row[0] or 0),
            "pending_events": int(row[1] or 0),
            "failed_events": int(row[2] or 0),
        }

    async def prune_exchange_events(self, retention_days: int) -> int:
        cutoff = int((_t.time() - max(1, retention_days) * 86400) * 1000)
        async with self._sessionmaker() as session:
            result = await session.execute(delete(ExchangeEventRow).where(
                ExchangeEventRow.status == "applied",
                ExchangeEventRow.received_at_ms < cutoff,
            ))
            await session.commit()
            return int(result.rowcount or 0)

    async def record_state_drift(
        self,
        *,
        entity_type: str,
        entity_key: str,
        reason: str,
        projection: Any,
        rest: Any,
        resolved: bool = True,
    ) -> None:
        await self._add(ExchangeStateDriftRow(
            entity_type=entity_type,
            entity_key=entity_key,
            reason=reason[:500],
            projection_json=json.dumps(projection, default=str)[:50000],
            rest_json=json.dumps(rest, default=str)[:50000],
            resolved=resolved,
        ))

    async def recent_drift_count(self, since_ms: int) -> int:
        async with self._sessionmaker() as session:
            return int((await session.execute(
                select(text("COUNT(*)")).select_from(ExchangeStateDriftRow).where(
                    ExchangeStateDriftRow.ts_ms >= since_ms
                )
            )).scalar_one() or 0)

    async def mark_exchange_event_applied(self, event_key: str) -> None:
        async with self._sessionmaker() as session:
            row = (await session.execute(
                select(ExchangeEventRow).where(ExchangeEventRow.event_key == event_key)
            )).scalars().first()
            if row:
                row.status = "applied"
                row.applied_at_ms = int(_t.time() * 1000)
                await session.commit()

    async def mark_exchange_event_failed(self, event_key: str, error: str) -> None:
        async with self._sessionmaker() as session:
            row = (await session.execute(
                select(ExchangeEventRow).where(ExchangeEventRow.event_key == event_key)
            )).scalars().first()
            if row:
                row.status = "failed"
                row.error = error[:500]
                await session.commit()

    async def record_stream_health(self, health: dict[str, Any]) -> None:
        session_id = str(health.get("session_id") or "current")
        now = int(health.get("ts_ms") or _t.time() * 1000)
        status = str(health.get("status") or "UNKNOWN")
        async with self._sessionmaker() as session:
            row = await session.get(ExchangeStreamSessionRow, session_id)
            if row is None:
                row = ExchangeStreamSessionRow(session_id=session_id)
                session.add(row)
            row.status = status
            row.listen_key_hash = str(health.get("listen_key_hash") or row.listen_key_hash)
            row.reason = str(health.get("reason") or "")[:500]
            row.updated_at_ms = now
            if health.get("connected_at_ms"):
                row.connected_at_ms = int(health["connected_at_ms"])
            if health.get("keepalive_at_ms"):
                row.keepalive_at_ms = int(health["keepalive_at_ms"])
            if health.get("last_resync_at_ms"):
                row.last_resync_at_ms = int(health["last_resync_at_ms"])
            if status == "DISCONNECTED":
                row.disconnected_at_ms = now
            await session.commit()
        await self.set_runtime_settings({
            "stream.status": status,
            "stream.reason": str(health.get("reason") or ""),
            "stream.session_id": session_id,
            "stream.updated_at_ms": str(now),
            "stream.last_resync_at_ms": str(health.get("last_resync_at_ms") or ""),
        })

    async def upsert_live_balance(self, balance: dict[str, Any], source: str) -> None:
        asset = str(balance.get("asset") or "")
        if not asset:
            return
        async with self._sessionmaker() as session:
            row = await session.get(LiveBalanceRow, asset) or LiveBalanceRow(asset=asset)
            session.add(row)
            row.wallet_balance = float(balance.get("wallet_balance") or 0)
            row.available_balance = float(balance.get("available_balance") or 0)
            row.source = source
            row.updated_at_ms = int(balance.get("ts_ms") or _t.time() * 1000)
            row.raw_json = json.dumps(balance, default=str)[:8000]
            await session.commit()

    async def upsert_live_position(self, position: dict[str, Any], source: str) -> None:
        pos = normalize_position(position)
        if not pos["symbol"]:
            return
        async with self._sessionmaker() as session:
            row = await session.get(LivePositionRow, pos["symbol"]) or LivePositionRow(
                symbol=pos["symbol"]
            )
            session.add(row)
            for key in ("side", "contracts", "entry_price", "mark_price", "leverage",
                        "unrealized_pnl", "notional"):
                setattr(row, key, pos[key])
            row.source = source
            row.updated_at_ms = int(pos.get("ts_ms") or _t.time() * 1000)
            row.raw_json = json.dumps(position, default=str)[:8000]
            await session.commit()

    async def delete_live_position(self, symbol: str) -> None:
        async with self._sessionmaker() as session:
            await session.execute(delete(LivePositionRow).where(LivePositionRow.symbol == symbol))
            await session.commit()

    async def upsert_live_order(
        self, order: dict[str, Any], order_class: str, source: str
    ) -> None:
        exchange_id = str(order.get("id") or "")
        if not exchange_id:
            return
        async with self._sessionmaker() as session:
            row = (await session.execute(select(LiveOrderRow).where(
                LiveOrderRow.order_class == order_class,
                LiveOrderRow.exchange_order_id == exchange_id,
            ))).scalars().first()
            if row is None:
                row = LiveOrderRow(order_class=order_class, exchange_order_id=exchange_id)
                session.add(row)
            row.client_order_id = str(
                order.get("client_order_id") or order.get("client_algo_id") or ""
            )
            row.symbol = str(order.get("symbol") or "")
            row.kind = str(order.get("kind") or "")
            row.side = str(order.get("side") or "")
            row.order_type = str(order.get("order_type") or "")
            row.qty = float(order.get("qty") or 0)
            row.filled_qty = float(order.get("filled_qty") or 0)
            row.price = float(order.get("price") or order.get("avg_price") or 0)
            row.trigger_price = float(order.get("trigger_price") or 0)
            row.status = str(order.get("status") or "")
            row.reduce_only = bool(order.get("reduce_only"))
            row.source = source
            row.updated_at_ms = int(order.get("ts_ms") or _t.time() * 1000)
            row.raw_json = json.dumps(order, default=str)[:8000]
            await session.commit()

    async def replace_live_account(
        self,
        *,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        balances: list[dict[str, Any]],
        source: str,
        ts_ms: int,
    ) -> None:
        async with self._sessionmaker() as session:
            await session.execute(delete(LivePositionRow))
            await session.execute(delete(LiveOrderRow))
            await session.commit()
        for position in positions:
            await self.upsert_live_position(position, source)
        for order in orders:
            order_class = "algo" if order.get("kind") in ("SL", "TP") else "regular"
            await self.upsert_live_order(order, order_class, source)
        for balance in balances:
            await self.upsert_live_balance({**balance, "ts_ms": ts_ms}, source)

    async def live_account_state(self) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            positions = (await session.execute(select(LivePositionRow))).scalars().all()
            orders = (await session.execute(select(LiveOrderRow))).scalars().all()
            balances = (await session.execute(select(LiveBalanceRow))).scalars().all()
        return {
            "positions": [
                {c.name: getattr(row, c.name) for c in LivePositionRow.__table__.columns
                 if c.name != "raw_json"} for row in positions
            ],
            "open_orders": [
                {c.name: getattr(row, c.name) for c in LiveOrderRow.__table__.columns
                 if c.name not in ("raw_json", "id")}
                for row in orders if row.status in ("placed", "new", "open", "working", "partial")
            ],
            "balances": [
                {c.name: getattr(row, c.name) for c in LiveBalanceRow.__table__.columns
                 if c.name != "raw_json"} for row in balances
            ],
        }

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
            "disabled_reason_code": row.disabled_reason_code,
            "disabled_reason": row.disabled_reason,
            "disabled_at": row.disabled_at,
            "disabled_source": row.disabled_source,
            "disabled_action": row.disabled_action,
            "last_enabled_at": row.last_enabled_at,
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

    async def set_symbol_enabled(
        self,
        symbol: str,
        enabled: bool,
        *,
        reason_code: str = "",
        reason: str = "",
        source: str = "",
        action: str = "",
    ) -> None:
        symbol = normalize_symbol(symbol)
        now = _now_iso_utc()
        async with self._sessionmaker() as session:
            row = await session.get(SymbolRow, symbol)
            if row is None or row.status == "archived":
                raise ValueError(f"symbol not registered: {symbol}")
            row.enabled = enabled
            row.updated_at = now
            if enabled:
                row.disabled_reason_code = ""
                row.disabled_reason = ""
                row.disabled_at = ""
                row.disabled_source = ""
                row.disabled_action = ""
                row.last_enabled_at = now
            else:
                row.disabled_reason_code = (reason_code or "DISABLED")[:64]
                row.disabled_reason = (reason or "")[:2000]
                row.disabled_at = now
                row.disabled_source = (source or "")[:32]
                row.disabled_action = (action or "")[:32]
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

    # ---------- 仓位所有权声明 ----------
    async def begin_position_claim(
        self,
        *,
        symbol: str,
        side: str,
        planned_qty: float,
        source: str = "strategy",
        ttl_ms: int = 300_000,
        reason: str = "",
    ) -> int:
        """在发出交易所开仓前声明本地所有权，避免部分成交竞态误判为外部仓位。"""
        import time as _t

        symbol = normalize_symbol(symbol)
        now_ms = int(_t.time() * 1000)
        now = _now_iso_utc()
        async with self._sessionmaker() as session:
            row = PositionClaimRow(
                symbol=symbol,
                side=(side or "").lower(),
                status="opening",
                source=source[:16],
                planned_qty=planned_qty,
                expires_at_ms=now_ms + max(int(ttl_ms), 1),
                reason=reason[:240],
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            claim_id = row.id
            await session.commit()
            return claim_id

    async def finish_position_claim(
        self,
        claim_id: int,
        *,
        status: str,
        filled_qty: float = 0.0,
        entry_price: float = 0.0,
        client_order_id: str = "",
        reason: str = "",
        raw: dict[str, Any] | None = None,
    ) -> None:
        now = _now_iso_utc()
        async with self._sessionmaker() as session:
            row = await session.get(PositionClaimRow, claim_id)
            if row is None:
                return
            row.status = status[:24]
            row.filled_qty = _safe_float(filled_qty)
            row.entry_price = _safe_float(entry_price)
            if client_order_id:
                row.client_order_id = client_order_id[:64]
            row.reason = reason[:240]
            row.updated_at = now
            if raw is not None:
                try:
                    row.raw_json = json.dumps(raw, ensure_ascii=False, default=str)[:8000]
                except Exception:
                    row.raw_json = ""
            await session.commit()

    async def latest_finished_position_claim(
        self,
        symbol: str,
        *,
        within_ms: int = 900_000,
    ) -> dict[str, Any] | None:
        """B4 修复：最近一条已收尾的 position_claim（含 canceled/error/filled）。

        用于判断「交易所确实有 0<qty<planned 的孤儿持仓」是否来源于最近一次
        策略 OPEN 失败但留了部分成交的 MAKER 序列，从而走「接管」路径而不是
        「禁用币种」路径。

        within_ms: 仅返回 ts_ms 在此窗口内的记录。默认 15 分钟。
        updated_at 是 String('YYYY-MM-DD HH:MM:SS')，因此用 ts_ms 做窗口过滤。
        """
        import time as _t

        symbol = normalize_symbol(symbol)
        now_ms = int(_t.time() * 1000)
        threshold = now_ms - max(int(within_ms), 1)
        finished_statuses = {"canceled", "error", "filled", "partial", "rejected", "expired"}
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(PositionClaimRow)
                    .where(PositionClaimRow.symbol == symbol)
                    .where(PositionClaimRow.status.in_(tuple(finished_statuses)))
                    .where(PositionClaimRow.ts_ms >= threshold)
                    .order_by(PositionClaimRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "ts_ms": row.ts_ms,
                "symbol": row.symbol,
                "side": row.side,
                "status": row.status,
                "source": row.source,
                "planned_qty": row.planned_qty,
                "filled_qty": row.filled_qty,
                "entry_price": row.entry_price,
                "client_order_id": row.client_order_id,
                "reason": row.reason,
            }

    async def day_realized_pnl_by_local_day(self) -> dict[str, float]:
        """按本地时区日界（凌晨 0:00 滚动）聚合 trades.net_realized_pnl。

        返回 {YYYY-MM-DD: pnl}，仅包含 closed_at_ms > 0 的 trade。供启动时把
        runtime.day_realized_pnl 重新对齐到 DB 真实数据，避免重启后日亏熔断
        与前端"当日已实现盈亏"失真（详见
        docs/ops/2026-06-09-day-pnl-rehydrate.md）。
        """
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(
                        TradeRow.closed_at_ms,
                        TradeRow.net_realized_pnl,
                    ).where(TradeRow.closed_at_ms > 0)
                )
            ).all()
        out: dict[str, float] = {}
        for ts_ms, pnl in rows:
            local = time.localtime(int(ts_ms) / 1000.0)
            key = f"{local.tm_year:04d}-{local.tm_mon:02d}-{local.tm_mday:02d}"
            out[key] = out.get(key, 0.0) + float(pnl or 0.0)
        return out

    async def has_active_position_claim(self, symbol: str) -> bool:
        import time as _t

        symbol = normalize_symbol(symbol)
        now_ms = int(_t.time() * 1000)
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(PositionClaimRow.id)
                    .where(PositionClaimRow.symbol == symbol)
                    .where(PositionClaimRow.status.in_(tuple(_ACTIVE_CLAIM_STATUSES)))
                    .where(PositionClaimRow.expires_at_ms >= now_ms)
                    .order_by(PositionClaimRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row is not None

    async def has_recent_entry_claim(self, symbol: str) -> bool:
        """Return True while a recent entry claim can still race exchange flat checks."""
        import time as _t

        symbol = normalize_symbol(symbol)
        now_ms = int(_t.time() * 1000)
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(PositionClaimRow.id)
                    .where(PositionClaimRow.symbol == symbol)
                    .where(PositionClaimRow.status.in_(tuple(_RECENT_ENTRY_CLAIM_STATUSES)))
                    .where(PositionClaimRow.expires_at_ms >= now_ms)
                    .where(
                        or_(
                            PositionClaimRow.status.in_(tuple(_ACTIVE_CLAIM_STATUSES)),
                            PositionClaimRow.filled_qty > 0,
                        )
                    )
                    .order_by(PositionClaimRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row is not None

    async def has_fresh_open_trade(self, symbol: str, max_age_ms: int) -> bool:
        """Return True for newly opened local trades still inside the flat grace window."""
        import time as _t

        symbol = normalize_symbol(symbol)
        threshold = int(_t.time() * 1000) - max(int(max_age_ms), 0)
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(TradeRow.id)
                    .where(TradeRow.symbol == symbol)
                    .where(TradeRow.status.in_(tuple(_OPEN_TRADE_STATUSES)))
                    .where(TradeRow.opened_at_ms >= threshold)
                    .order_by(TradeRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row is not None

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
        llm_prompt: str = "",
        llm_request_json: str = "",
        llm_response_json: str = "",
        feature_snapshot_json: str = "",
        llm_latency_ms: int = 0,
        llm_attempts: int = 0,
        llm_status: str = "",
        llm_error: str = "",
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
            row.reason = decision.reason[:DECISION_REASON_MAX_LENGTH]
        if ctx is not None:
            try:
                row.context_json = ctx.model_dump_json()
            except Exception:  # 落库失败不能影响主流程
                row.context_json = ""
        row.llm_prompt = llm_prompt
        row.llm_request_json = llm_request_json
        row.llm_response_json = llm_response_json
        row.feature_snapshot_json = feature_snapshot_json
        row.llm_latency_ms = max(0, int(llm_latency_ms or 0))
        row.llm_attempts = max(0, int(llm_attempts or 0))
        row.llm_status = (llm_status or "")[:16]
        row.llm_error = (llm_error or "")[:200]
        await self._add(row)

    async def log_audit(self, *, symbol: str, action: str, reason: str = "") -> None:
        """轻量审计行（不是决策、也不是拒单）。例如 LLM profile 切换记录。"""
        row = DecisionRow(
            symbol=symbol[:20],
            skipped=False,
            action=action[:16],
            reason=reason[:DECISION_REASON_MAX_LENGTH],
        )
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
            await self._attach_order_to_trade(
                session,
                row,
                source=str(order.get("source") or "live")[:16],
                confidence=str(order.get("confidence") or "exact")[:16],
            )
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
            execution_mode=str(order.get("execution_mode") or ""),
            time_in_force=str(order.get("time_in_force") or ""),
            requested_qty=_safe_float(order.get("requested_qty")),
            filled_qty=_safe_float(order.get("filled_qty")),
            remaining_qty=_safe_float(order.get("remaining_qty")),
            requested_price=_safe_float(order.get("requested_price")),
            limit_price=_safe_float(order.get("limit_price")),
            avg_price=_safe_float(order.get("avg_price")),
            liquidity=str(order.get("liquidity") or ""),
            fee=_safe_float(order.get("fee")),
            fee_asset=str(order.get("fee_asset") or ""),
            client_order_id=str(order.get("client_order_id") or ""),
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
            if status in _TRIGGERED_CONDITION_STATUSES:
                await self._close_open_trades_with_exit_order(
                    session, row, exit_reason=kind
                )
                return
            trade = await self._find_open_trade_for_order(
                session, row, allow_fallback=False
            )
            if trade is None:
                trade = await self._find_recent_trade_for_condition(session, row)
            if trade is None:
                row.trade_role = _trade_role(kind)
                return
            row.trade_id = trade.id
            row.trade_role = _trade_role(kind)
            row.leverage = trade.leverage
            row.margin = _margin(row.notional, trade.leverage)
            return

        if kind == "CLOSE":
            if status in _FILLED_ORDER_STATUSES:
                await self._close_open_trades_with_exit_order(
                    session, row, exit_reason="CLOSE"
                )

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
            entry_fee=row.fee,
            total_fee=row.fee,
            entry_liquidity=row.liquidity,
            source=source,
            confidence=confidence,
        )
        session.add(trade)
        await session.flush()
        return trade

    async def _find_open_trade_for_order(
        self,
        session: AsyncSession,
        row: OrderRow,
        *,
        allow_fallback: bool = True,
    ) -> TradeRow | None:
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
        return rows[0] if allow_fallback and rows else None

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

    async def _close_open_trades_with_exit_order(
        self,
        session: AsyncSession,
        row: OrderRow,
        *,
        exit_reason: str,
    ) -> None:
        """把一个聚合退出成交按 FIFO 分摊到本地 open trades。

        交易所是 symbol/side 聚合仓位；一张 reduce-only CLOSE/SL/TP 可能覆盖多个
        本地 trade lot。这里按开仓时间分摊，避免把整张退出单只挂到最新 takeover
        trade 上，导致旧 maker 部分成交长期显示 open。
        """
        direction = _direction_from_close_side(row.side)
        if not direction:
            return
        stmt = (
            select(TradeRow)
            .where(TradeRow.symbol == row.symbol)
            .where(TradeRow.direction == direction)
            .where(TradeRow.status.in_(tuple(_OPEN_TRADE_STATUSES)))
            .where(TradeRow.dry_run.is_(row.dry_run))
            .where(TradeRow.opened_at_ms <= row.ts_ms)
            .order_by(TradeRow.opened_at_ms.asc(), TradeRow.id.asc())
        )
        trades = (await session.execute(stmt)).scalars().all()
        if row.trade_id:
            preferred = await session.get(TradeRow, row.trade_id)
            if (
                preferred is not None
                and preferred.status in _OPEN_TRADE_STATUSES
                and preferred.symbol == row.symbol
                and preferred.direction == direction
            ):
                trades = [preferred] + [trade for trade in trades if trade.id != preferred.id]
        if not trades:
            return

        requested = row.qty if row.qty > 0 else sum(
            max(float(trade.qty_opened or 0.0) - float(trade.qty_closed or 0.0), 0.0)
            for trade in trades
        )
        if requested <= 0:
            return

        remaining = requested
        first_trade: TradeRow | None = None
        row.realized_pnl = 0.0
        for trade in trades:
            open_qty = max(float(trade.qty_opened or 0.0) - float(trade.qty_closed or 0.0), 0.0)
            if open_qty <= 0:
                continue
            alloc_qty = min(open_qty, remaining)
            if alloc_qty <= 0:
                continue
            fee_alloc = row.fee * (alloc_qty / requested) if row.fee and requested > 0 else 0.0
            self._close_trade_with_order(
                trade,
                row,
                exit_reason=exit_reason,
                qty=alloc_qty,
                fee=fee_alloc,
            )
            if first_trade is None:
                first_trade = trade
            remaining -= alloc_qty
            if remaining <= max(requested * 1e-6, 1e-12):
                break

        if first_trade is not None:
            row.trade_id = first_trade.id
            row.trade_role = "EXIT" if row.client_kind == "CLOSE" else _trade_role(row.client_kind)
            row.leverage = first_trade.leverage
            row.margin = _margin(row.notional, first_trade.leverage)

    def _close_trade_with_order(
        self,
        trade: TradeRow,
        row: OrderRow,
        *,
        exit_reason: str,
        qty: float | None = None,
        fee: float | None = None,
    ) -> None:
        qty = qty if qty is not None else row.qty
        qty = qty if qty > 0 else max(trade.qty_opened - trade.qty_closed, 0.0)
        if qty <= 0:
            return
        exit_price = _raw_number(row.raw_json, "filled_price") or row.price
        pnl = _realized_pnl(
            direction=trade.direction,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            qty=qty,
        )
        fee = row.fee if fee is None else fee
        margin = trade.entry_margin or _margin(trade.entry_notional, trade.leverage)
        prev_closed = trade.qty_closed if trade.qty_closed > 0 else 0.0
        total_closed = min(trade.qty_opened, prev_closed + qty)
        is_closed = self._qty_matches(trade.qty_opened, total_closed)
        trade.status = "closed" if is_closed else "partial"
        if is_closed:
            trade.closed_at_ms = row.ts_ms
            trade.closed_at = row.created_at
        trade.exit_order_id = row.id
        trade.exit_price = exit_price
        trade.qty_closed = total_closed
        trade.exit_notional += abs(qty * exit_price) if exit_price > 0 else row.notional
        trade.realized_pnl += pnl
        trade.gross_realized_pnl = trade.realized_pnl
        trade.exit_fee += fee
        trade.total_fee = trade.entry_fee + trade.exit_fee
        trade.net_realized_pnl = trade.gross_realized_pnl - trade.total_fee
        trade.pnl_pct_on_margin = _pnl_pct(trade.realized_pnl, margin)
        trade.net_pnl_pct_on_margin = _pnl_pct(trade.net_realized_pnl, margin)
        trade.exit_reason = exit_reason
        trade.exit_liquidity = row.liquidity or trade.exit_liquidity
        row.realized_pnl = (row.realized_pnl or 0.0) + pnl

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

    async def sync_condition_order_history(
        self,
        *,
        symbol: str,
        live_exchange_order_ids: set[str],
        history_orders: list[dict[str, Any]],
    ) -> int:
        """持仓仍存在时同步已不在 open 列表里的条件单终态。

        只在交易所历史明确返回 filled/triggered/canceled/expired 时更新，避免把 API
        分页缺失误判成取消。
        """
        symbol = normalize_symbol(symbol)
        live_ids = {str(x) for x in live_exchange_order_ids if str(x)}
        history_by_id = {
            str(order.get("id") or ""): order
            for order in history_orders
            if str(order.get("id") or "")
        }
        if not history_by_id:
            return 0
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(OrderRow)
                    .where(OrderRow.symbol == symbol)
                    .where(OrderRow.client_kind.in_(("SL", "TP")))
                    .where(OrderRow.dry_run.is_(False))
                    .where(OrderRow.status.in_(("placed", "open")))
                )
            ).scalars().all()
            changed = 0
            for row in rows:
                if row.exchange_order_id and row.exchange_order_id in live_ids:
                    continue
                hist = history_by_id.get(str(row.exchange_order_id or ""))
                if not hist:
                    continue
                status = str(hist.get("status") or "").lower()
                if status in _TRIGGERED_CONDITION_STATUSES:
                    row.status = "filled"
                    qty = _safe_float(hist.get("filled_qty")) or _safe_float(hist.get("qty")) or row.qty
                    price = (
                        _safe_float(hist.get("filled_price"))
                        or _safe_float(hist.get("avg_price"))
                        or _safe_float(hist.get("price"))
                        or _safe_float(hist.get("trigger_price"))
                        or row.price
                    )
                    if qty > 0:
                        row.qty = qty
                    if price > 0:
                        row.notional = abs(row.qty * price)
                    self._merge_order_raw(row, {"condition_history": hist, "filled_price": price, "filled_qty": row.qty})
                    await self._refresh_trade_for_existing_order(session, row)
                    if row.trade_id:
                        counterparts = (
                            await session.execute(
                                select(OrderRow)
                                .where(OrderRow.trade_id == row.trade_id)
                                .where(OrderRow.id != row.id)
                                .where(OrderRow.client_kind.in_(("SL", "TP")))
                                .where(OrderRow.status.in_(("placed", "open")))
                            )
                        ).scalars().all()
                        for other in counterparts:
                            if other.exchange_order_id and other.exchange_order_id in live_ids:
                                continue
                            other.status = "canceled"
                            self._merge_order_raw(other, {"canceled_by_condition": row.exchange_order_id})
                    changed += 1
                    continue
                if status in {"canceled", "cancelled", "expired", "rejected"}:
                    row.status = "canceled" if status == "cancelled" else status
                    self._merge_order_raw(row, {"condition_history": hist})
                    await self._refresh_trade_for_existing_order(session, row)
                    changed += 1
            await session.commit()
            return changed

    @staticmethod
    def _merge_order_raw(row: OrderRow, extra: dict[str, Any]) -> None:
        try:
            raw = json.loads(row.raw_json or "{}")
            if not isinstance(raw, dict):
                raw = {"raw": raw}
        except Exception:
            raw = {}
        raw.update(extra)
        try:
            row.raw_json = json.dumps(raw, ensure_ascii=False, default=str)[:8000]
        except Exception:
            pass

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

    async def latest_decision_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Return latest persisted feature snapshot for LLM throttle restoration."""
        symbol = normalize_symbol(symbol)
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(DecisionRow)
                    .where(DecisionRow.symbol == symbol)
                    .where(DecisionRow.skipped.is_(False))
                    .where(DecisionRow.feature_snapshot_json != "")
                    .order_by(DecisionRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            try:
                snap = json.loads(row.feature_snapshot_json or "{}")
            except Exception:
                return None
            if not isinstance(snap, dict):
                return None
            return {
                "decision_id": row.id,
                "ts_ms": row.ts_ms,
                "ref_price": row.ref_price,
                "snapshot": snap,
            }

    async def has_open_trade(self, symbol: str) -> bool:
        """Return whether this symbol has a local open trade lifecycle."""
        symbol = normalize_symbol(symbol)
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(TradeRow.id)
                    .where(TradeRow.symbol == symbol)
                    .where(TradeRow.status.in_(tuple(_OPEN_TRADE_STATUSES)))
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row is not None

    async def open_trade_qty(self, symbol: str) -> float:
        """返回本地仍打开的 managed 数量合计，用于识别交易所剩余/人工仓位。"""
        symbol = normalize_symbol(symbol)
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(TradeRow)
                    .where(TradeRow.symbol == symbol)
                    .where(TradeRow.status.in_(tuple(_OPEN_TRADE_STATUSES)))
                )
            ).scalars().all()
            qty = 0.0
            for row in rows:
                qty += max(float(row.qty_opened or 0.0) - float(row.qty_closed or 0.0), 0.0)
            return qty

    async def reconcile_symbol_flat(
        self,
        symbol: str,
        *,
        reason: str = "EXCHANGE_FLAT",
        opened_before_ms: int | None = None,
        min_open_age_ms: int = 0,
        exchange_trades_provider: (
            "Callable[[str, int, int], Awaitable[list[dict]]] | None"
        ) = None,
    ) -> int:
        """交易所确认该币种无持仓时，关闭仍悬挂的本地 open trade。

        这是兜底账务修复：优先用该 trade 开仓后的最近退出成交价（本地 close 订单
        的 filled_price/avgPx）；如果本地没有可用退出订单，调用方可以注入
        ``exchange_trades_provider(symbol, since_ms, until_ms)`` 直接拉交易所 myTrades
        反查真实平仓均价；两者都拿不到时退回到用入场价关闭，并把 confidence 标记为
        ``inferred``，避免页面继续显示虚假持仓。
        """
        import time as _t

        symbol = normalize_symbol(symbol)
        now_ms = int(_t.time() * 1000)
        now = _now_iso_utc()
        async with self._sessionmaker() as session:
            stmt = (
                select(TradeRow)
                .where(TradeRow.symbol == symbol)
                .where(TradeRow.status.in_(tuple(_OPEN_TRADE_STATUSES)))
            )
            if opened_before_ms is not None:
                stmt = stmt.where(TradeRow.opened_at_ms <= int(opened_before_ms))
            if min_open_age_ms > 0:
                stmt = stmt.where(TradeRow.opened_at_ms <= now_ms - int(min_open_age_ms))
            trades = (
                await session.execute(
                    stmt.order_by(TradeRow.opened_at_ms.asc(), TradeRow.id.asc())
                )
            ).scalars().all()
            changed = 0
            for trade in trades:
                open_qty = max(
                    float(trade.qty_opened or 0.0) - float(trade.qty_closed or 0.0),
                    0.0,
                )
                if open_qty <= 0:
                    continue
                exit_row = await self._latest_exit_order_after_open(session, trade)
                exit_price = (
                    (_raw_number(exit_row.raw_json, "filled_price") if exit_row else 0.0)
                    or (float(exit_row.price or 0.0) if exit_row else 0.0)
                    or float(trade.entry_price or 0.0)
                )
                exit_source = "local_close_order" if exit_row else "inferred_entry"
                exit_fee = float(exit_row.fee or 0.0) if exit_row else 0.0
                # 本地没有 close 订单时（典型：交易所侧强平/止损触发的 EXCHANGE_FLAT），
                # 退回到入场价会让 pnl 算成 0。允许调用方注入 provider 直接反查
                # 交易所 myTrades 拿真实平仓均价；provider 异常/空数据时安全降级。
                if (
                    exit_row is None
                    and exchange_trades_provider is not None
                    and float(trade.entry_price or 0.0) > 0
                ):
                    try:
                        fetched = await exchange_trades_provider(
                            symbol, int(trade.opened_at_ms), now_ms
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "{} reconcile exit price provider failed, fallback to entry: {}",
                            symbol, e,
                        )
                        fetched = None
                    if fetched:
                        real_exit = _weighted_exit_price_from_trades(
                            fetched,
                            direction=trade.direction,
                            since_ms=int(trade.opened_at_ms),
                            target_qty=open_qty,
                        )
                        if real_exit > 0:
                            exit_price = real_exit
                            exit_source = "exchange_my_trades"
                            exit_fee = _sum_fee_from_trades(
                                fetched,
                                direction=trade.direction,
                                since_ms=int(trade.opened_at_ms),
                                target_qty=open_qty,
                            )
                pnl = _realized_pnl(
                    direction=trade.direction,
                    entry_price=trade.entry_price,
                    exit_price=exit_price,
                    qty=open_qty,
                )
                trade.status = "closed"
                trade.closed_at_ms = int(exit_row.ts_ms if exit_row else now_ms)
                trade.closed_at = str(exit_row.created_at if exit_row else now)
                trade.exit_order_id = int(exit_row.id if exit_row else 0)
                trade.exit_price = exit_price
                trade.qty_closed = float(trade.qty_opened or 0.0)
                trade.exit_notional += abs(open_qty * exit_price)
                trade.realized_pnl += pnl
                trade.gross_realized_pnl = trade.realized_pnl
                # 当 exit 价来自交易所 myTrades、且本地 close 订单缺失时，平仓手续费
                # 同步从 myTrades 累加（避免 total_fee 漏算退场手续费）。
                if exit_row is None and exit_source == "exchange_my_trades" and exit_fee > 0:
                    trade.exit_fee = float(trade.exit_fee or 0.0) + exit_fee
                    trade.total_fee = float(trade.entry_fee or 0.0) + trade.exit_fee
                trade.net_realized_pnl = trade.gross_realized_pnl - trade.total_fee
                margin = trade.entry_margin or _margin(trade.entry_notional, trade.leverage)
                trade.pnl_pct_on_margin = _pnl_pct(trade.realized_pnl, margin)
                trade.net_pnl_pct_on_margin = _pnl_pct(trade.net_realized_pnl, margin)
                trade.exit_reason = reason[:24]
                trade.confidence = "inferred"
                logger.info(
                    "{} reconcile flat trade={} pnl={:.4f} fee={:.4f} source={} prev_pnl={:.4f}",
                    symbol, trade.id, pnl, exit_fee, exit_source,
                    float(trade.realized_pnl or 0.0) - pnl,
                )
                changed += 1
            await session.commit()
            return changed

    async def _latest_exit_order_after_open(
        self,
        session: AsyncSession,
        trade: TradeRow,
    ) -> OrderRow | None:
        close_side = "sell" if trade.direction == "long" else "buy"
        return (
            await session.execute(
                select(OrderRow)
                .where(OrderRow.symbol == trade.symbol)
                .where(OrderRow.side == close_side)
                .where(OrderRow.client_kind.in_(("CLOSE", "SL", "TP")))
                .where(OrderRow.status.in_(tuple(_FILLED_ORDER_STATUSES | _TRIGGERED_CONDITION_STATUSES)))
                .where(or_(OrderRow.trade_id == 0, OrderRow.trade_id == trade.id))
                .where(OrderRow.ts_ms >= trade.opened_at_ms)
                .order_by(OrderRow.ts_ms.desc(), OrderRow.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def ensure_takeover_trade(
        self,
        *,
        symbol: str,
        direction: str,
        qty: float,
        entry_price: float,
        leverage: int = 0,
        source: str = "takeover",
    ) -> int:
        """为人工确认接管的剩余仓位创建或复用一条 open trade。"""
        symbol = normalize_symbol(symbol)
        direction = (direction or "").lower()
        qty = _safe_float(qty)
        entry_price = _safe_float(entry_price)
        async with self._sessionmaker() as session:
            existing = (
                await session.execute(
                    select(TradeRow)
                    .where(TradeRow.symbol == symbol)
                    .where(TradeRow.direction == direction)
                    .where(TradeRow.status.in_(tuple(_OPEN_TRADE_STATUSES)))
                    .where(TradeRow.source == source)
                    .order_by(TradeRow.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None and self._qty_matches(existing.qty_opened, qty):
                return existing.id

            import time as _t

            now_ms = int(_t.time() * 1000)
            now = _now_iso_utc()
            notional = abs(qty * entry_price)
            trade = TradeRow(
                ts_ms=now_ms,
                created_at=now,
                symbol=symbol,
                direction=direction,
                status="open",
                dry_run=False,
                opened_at_ms=now_ms,
                opened_at=now,
                entry_price=entry_price,
                qty_opened=qty,
                leverage=leverage,
                entry_notional=notional,
                entry_margin=_margin(notional, leverage),
                source=source[:16],
                confidence="manual",
            )
            session.add(trade)
            await session.flush()
            trade_id = trade.id
            await session.commit()
            return trade_id

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


    # ---------- LLM profile 持久化 ----------
    # 设计要点：
    # - api_key 明文存库（单租户自托管）；对外视图脱敏（key_present + 末4位 mask）。
    # - 同一时刻 is_active 只能为 True，由 upsert_llm_profile + activate_llm_profile 事务保证。
    # - list/get/activate 永远不返回 key 明文（key 也不在 ORM 字段里）。
    async def list_llm_profiles(self) -> list[dict[str, Any]]:
        """返回全部 LLM profile（不含 key 明文，仅 key_present + 末4位 mask）。"""
        async with self._sessionmaker() as session:
            rows = (await session.execute(
                select(LLMProfileRow).order_by(LLMProfileRow.name)
            )).scalars().all()
            return [self._profile_public(r) for r in rows]

    @staticmethod
    def _profile_public(r: LLMProfileRow) -> dict[str, Any]:
        """对外可见的 profile 视图：脱敏 key（绝不返明文）。"""
        api_key = r.api_key or ""
        return {
            "name": r.name,
            "provider": r.provider,
            "model": r.model,
            "base_url": r.base_url or "",
            "timeout": r.timeout,
            "max_tokens": r.max_tokens,
            "max_retries": r.max_retries,
            "is_active": bool(r.is_active),
            "priority": r.priority,
            "fallback_enabled": bool(r.fallback_enabled),
            "key_present": bool(api_key),
            "api_key_mask": ("****" + api_key[-4:]) if len(api_key) >= 4 else ("****" if api_key else ""),
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }

    async def get_llm_profile(self, name: str) -> dict[str, Any] | None:
        """获取单个 LLM profile（不含 key 明文）。"""
        async with self._sessionmaker() as session:
            r = await session.get(LLMProfileRow, name)
            if r is None:
                return None
            return self._profile_public(r)

    async def get_llm_profile_secret(self, name: str) -> str:
        """读取明文 api_key —— 仅 engine 建链 / web test 端点内部使用，绝不进 HTTP 响应。"""
        async with self._sessionmaker() as session:
            r = await session.get(LLMProfileRow, name)
            if r is None:
                raise KeyError(f"llm profile not found: {name!r}")
            return r.api_key or ""

    async def get_enabled_llm_profiles(self) -> list[dict[str, Any]]:
        """返回 fallback 链：active 主源 + 所有 fallback_enabled 备源。

        排序：priority 升序 → is_active 优先 → name。主源 activate 时 priority 置 0，恒为链头。
        """
        async with self._sessionmaker() as session:
            rows = (await session.execute(
                select(LLMProfileRow).where(
                    (LLMProfileRow.is_active == True) |  # noqa: E712
                    (LLMProfileRow.fallback_enabled == True)  # noqa: E712
                )
            )).scalars().all()
            ordered = sorted(
                rows, key=lambda r: (r.priority, 0 if r.is_active else 1, r.name)
            )
            return [self._profile_public(r) for r in ordered]

    async def upsert_llm_profile(
        self,
        *,
        name: str,
        provider: str,
        model: str,
        base_url: str | None,
        timeout: float,
        max_tokens: int,
        max_retries: int,
        api_key: str = "",
        priority: int = 100,
        fallback_enabled: bool = False,
    ) -> dict[str, Any]:
        """插入或更新一条 profile。is_active 不会被这里修改。

        ``api_key`` 留空表示不更新（PUT 不改 key 语义）；非空才覆盖。
        """
        import time as _t
        now = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime())
        async with self._sessionmaker() as session:
            row = await session.get(LLMProfileRow, name)
            if row is None:
                row = LLMProfileRow(
                    name=name,
                    provider=provider,
                    model=model,
                    base_url=base_url or "",
                    timeout=timeout,
                    max_tokens=max_tokens,
                    max_retries=max_retries,
                    api_key=api_key,
                    priority=priority,
                    fallback_enabled=fallback_enabled,
                    is_active=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.provider = provider
                row.model = model
                row.base_url = base_url or ""
                row.timeout = timeout
                row.max_tokens = max_tokens
                row.max_retries = max_retries
                row.priority = priority
                row.fallback_enabled = fallback_enabled
                # 留空表示不更新；非空才覆盖
                if api_key:
                    row.api_key = api_key
                row.updated_at = now
            await session.commit()
            return await self.get_llm_profile(name)

    async def delete_llm_profile(self, name: str) -> bool:
        """删除一条 profile；is_active 不会被自动转移。"""
        async with self._sessionmaker() as session:
            row = await session.get(LLMProfileRow, name)
            if row is None:
                return False
            if bool(row.is_active):
                raise ValueError(
                    f"cannot delete active profile {name!r}; switch first"
                )
            await session.delete(row)
            await session.commit()
            return True

    async def activate_llm_profile(self, name: str) -> dict[str, Any]:
        """把 is_active 标志从旧的切到 name；事务内互斥。新主源 priority 置 0 恒为链头。

        返回新的 active profile（不含 key 明文）。
        找不到 name 时抛 ValueError，事务回滚。
        """
        import time as _t
        now = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime())
        async with self._sessionmaker() as session:
            target = await session.get(LLMProfileRow, name)
            if target is None:
                raise ValueError(f"llm profile not found: {name!r}")
            # 取消其它 is_active
            others = (await session.execute(
                select(LLMProfileRow).where(LLMProfileRow.is_active == True)  # noqa: E712
            )).scalars().all()
            for r in others:
                if r.name != name:
                    r.is_active = False
                    r.updated_at = now
            target.is_active = True
            target.priority = 0
            target.updated_at = now
            await session.commit()
        prof = await self.get_llm_profile(name)
        assert prof is not None
        return prof

    async def get_active_llm_profile(self) -> dict[str, Any] | None:
        """返回当前 active profile（若无则 None）。"""
        async with self._sessionmaker() as session:
            row = (await session.execute(
                select(LLMProfileRow).where(LLMProfileRow.is_active == True)  # noqa: E712
            )).scalars().first()
            if row is None:
                return None
            return self._profile_public(row)


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

    async def record_system_command(
        self,
        command: str,
        *,
        arg: str = "",
        source: str = "engine",
        status: str = "done",
        result: str = "",
    ) -> int:
        """记录非 Web 入队的系统事件，让控制台命令历史可追溯。"""
        import time as _t
        now_ms = int(_t.time() * 1000)
        now = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime())
        row = ControlCommandRow(
            ts_ms=now_ms,
            created_at=now,
            command=command,
            arg=arg,
            source=source,
            status=status,
            result=result[:300],
            executed_at=now,
        )
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()
            return row.id

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
