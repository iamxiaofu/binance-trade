"""Binance/local fill comparison, canonical preview and atomic activation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text

from src.exchange.client import ExchangeClient
from src.exchange.fills import ccxt_trade_fill
from src.exchange.positions import normalize_position, normalize_symbol
from src.reconcile.binance_trades import (
    EPSILON,
    CanonicalFill,
    replay_trade_cycles,
    validate_replay,
)
from src.store.models import (
    BinanceTradeCycleFillRow,
    BinanceTradeCycleRow,
    ExchangeEventRow,
    ExchangeFillRow,
    ExchangeReconcileRunRow,
    RuntimeSettingRow,
)
from src.store.repo import Store


ACTIVE_RUN_SETTING = "binance.trade_cycles.active_run_id"
DEFAULT_DAYS = 30
MAX_DAYS = 90
WINDOW_MS = 6 * 24 * 60 * 60 * 1000


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) if ms else ""


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").lower() in {"1", "true", "yes"}


def _almost_equal(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    return abs(float(left or 0) - float(right or 0)) <= tolerance


class ReconcileError(RuntimeError):
    pass


class BinanceTradeReconciler:
    def __init__(
        self,
        store: Store,
        client: ExchangeClient,
        db_path: str,
        progress: Callable[[str, int, str], Awaitable[None]] | None = None,
    ):
        self.store = store
        self.client = client
        self.db_path = db_path
        self._progress_callback = progress
        self._progress_lock = asyncio.Lock()

    async def _progress(self, stage: str, pct: int, detail: str) -> None:
        if self._progress_callback is not None:
            async with self._progress_lock:
                await self._progress_callback(stage, pct, detail)

    async def preview(
        self,
        *,
        days: int = DEFAULT_DAYS,
        persist: bool = True,
        scope_start_ms: int | None = None,
    ) -> dict[str, Any]:
        days = min(max(int(days), 1), MAX_DAYS)
        await self._progress("initializing", 3, "获取 Binance 服务器时间")
        scope_end_ms = int(await self.client.fetch_server_time())
        if scope_start_ms is None:
            scope_start_ms = scope_end_ms - days * 24 * 60 * 60 * 1000
        await self._progress("local_fills", 8, "读取本地成交账本")
        local_rows = await self._local_fills(scope_start_ms, scope_end_ms)
        await self._progress("symbols", 15, "发现核对范围内交易币种")
        symbols = await self._scope_symbols(local_rows, scope_start_ms, scope_end_ms)
        await self._progress(
            "remote_fills", 25, f"拉取 Binance 成交，共 {len(symbols)} 个币种"
        )
        remote_rows = await self._remote_fills(symbols, scope_start_ms, scope_end_ms)
        await self._progress("verify_fills", 45, "校验本地与 Binance 成交集合")
        self._verify_remote_local(local_rows, remote_rows)

        await self._progress("event_metadata", 52, "读取私有流订单元数据")
        event_metadata = await self._event_order_metadata(scope_start_ms, scope_end_ms)
        cached_metadata = self._cached_order_metadata(local_rows)
        order_metadata = await self._fetch_order_metadata(
            local_rows, event_metadata, cached_metadata
        )
        await self._progress("resolve_ownership", 75, "解析订单归属与分批平仓关系")
        resolved = self._resolve_fills(
            local_rows, order_metadata, event_metadata, cached_metadata
        )
        unresolved = [
            row for row in resolved
            if row["ownership"] not in {"engine", "external"} or not row["client_order_id"]
        ]
        if unresolved:
            sample = ", ".join(
                f"{row['symbol']}:{row['exchange_trade_id']}" for row in unresolved[:10]
            )
            raise ReconcileError(f"存在无法确认归属的成交，拒绝修复：{sample}")

        await self._progress("replay", 82, "重建分批开仓和平仓周期")
        current_positions = await self._current_positions(symbols)
        initial_positions = self._derive_initial_positions(resolved, current_positions)
        canonical_fills = [CanonicalFill.from_mapping(row) for row in resolved]
        replay = replay_trade_cycles(canonical_fills, initial_positions=initial_positions)
        await self._progress("validate", 90, "校验重建仓位与交易所当前仓位")
        errors = validate_replay(canonical_fills, replay)
        errors.extend(self._validate_final_positions(replay.final_positions, current_positions))
        if errors:
            raise ReconcileError("；".join(errors[:20]))

        external_cycles = [
            cycle.public() for cycle in replay.cycles if cycle.ownership != "engine"
        ]
        old_external_count = await self._scalar("SELECT COUNT(*) FROM external_trades")
        ownership_changes = [
            {
                "fill_id": row["exchange_fill_id"],
                "symbol": row["symbol"],
                "trade_id": row["exchange_trade_id"],
                "before": row["original_ownership"],
                "after": row["ownership"],
            }
            for row in resolved
            if row["original_ownership"] != row["ownership"]
        ]
        metadata_changes = [
            row for row in resolved
            if (
                row["original_client_order_id"] != row["client_order_id"]
                or row["original_reduce_only"] != row["reduce_only"]
                or row["original_ownership"] != row["ownership"]
                or row["order_type"]
                or row["exit_reason"]
            )
        ]
        summary = {
            "scope": {
                "days": days,
                "start_ms": scope_start_ms,
                "end_ms": scope_end_ms,
                "symbols": symbols,
            },
            "fills": {
                "local": len(local_rows),
                "remote": len(remote_rows),
                "matched": len(local_rows),
                "ownership_changes": len(ownership_changes),
                "metadata_changes": len(metadata_changes),
                "unresolved": 0,
            },
            "cycles": {
                "before": int(old_external_count or 0),
                "after": len(external_cycles),
                "external": sum(1 for row in external_cycles if row["ownership"] == "external"),
                "mixed": sum(1 for row in external_cycles if row["ownership"] == "mixed"),
                "partial": sum(1 for row in external_cycles if row["status"] == "partial"),
                "open": sum(1 for row in external_cycles if row["status"] == "open"),
                "closed": sum(1 for row in external_cycles if row["status"] == "closed"),
            },
            "positions": {
                symbol: {
                    "exchange": float(current_positions.get(symbol, 0)),
                    "rebuilt": float(replay.final_positions.get(symbol, 0)),
                    "initial": float(initial_positions.get(symbol, 0)),
                }
                for symbol in symbols
            },
            "ownership_changes": ownership_changes[:100],
            "warnings": [
                f"{row['symbol']} 窗口开始前存在 {row['confidence']} 持仓"
                for row in external_cycles if row["confidence"] == "carry_in"
            ],
        }
        hash_payload = {
            "scope": {
                "days": days,
                "start_ms": scope_start_ms,
                "symbols": symbols,
            },
            "resolved_fills": [
                {
                    key: row[key]
                    for key in (
                        "exchange_fill_id", "ts_ms", "symbol", "exchange_trade_id",
                        "exchange_order_id", "client_order_id", "side", "qty", "price",
                        "fee", "realized_pnl", "reduce_only", "ownership", "order_type",
                        "exit_reason", "algo_id",
                    )
                }
                for row in resolved
            ],
            "cycles": external_cycles,
            "positions": summary["positions"],
        }
        preview_hash = hashlib.sha256(_canonical_json(hash_payload).encode()).hexdigest()
        result = {
            "preview_hash": preview_hash,
            "safe_to_apply": True,
            "summary": summary,
            "cycles": external_cycles,
            "_resolved_fills": resolved,
        }
        if persist:
            await self._progress("persist", 96, "保存不可变核对预览")
            result["run_id"] = await self._persist_preview(
                result, scope_start_ms=scope_start_ms, scope_end_ms=scope_end_ms
            )
        await self._progress("completed", 100, "核对预览完成")
        return result

    async def apply(self, *, run_id: int, preview_hash: str, days: int) -> dict[str, Any]:
        stored = await self._load_run(run_id)
        if stored is None or stored.preview_hash != preview_hash:
            raise ReconcileError("预览不存在或哈希不匹配")
        if stored.status == "applied":
            return {"applied": True, "run_id": run_id, "already_applied": True}

        fresh = await self.preview(
            days=days,
            persist=False,
            scope_start_ms=stored.scope_start_ms,
        )
        if fresh["preview_hash"] != preview_hash:
            raise ReconcileError("预览后交易所或本地成交已变化，请重新核对")
        backup_path = await asyncio.to_thread(self._backup_database)
        await self._activate_run(run_id, fresh["_resolved_fills"])
        return {
            "applied": True,
            "run_id": run_id,
            "preview_hash": preview_hash,
            "backup_path": backup_path,
            "summary": fresh["summary"],
        }

    async def _scope_symbols(
        self, local_rows: list[dict[str, Any]], scope_start_ms: int, scope_end_ms: int
    ) -> list[str]:
        symbols = {normalize_symbol(row["symbol"]) for row in local_rows}
        symbols.update(normalize_symbol(symbol) for symbol in self.client._settings.symbols)
        cursor = scope_start_ms
        one_day_ms = 24 * 60 * 60 * 1000
        while cursor <= scope_end_ms:
            window_end = min(cursor + one_day_ms - 1, scope_end_ms)
            try:
                income = await self.client.raw.fapiPrivateGetIncome({
                    "startTime": cursor,
                    "endTime": window_end,
                    "limit": 1000,
                })
            except Exception as exc:
                raise ReconcileError(f"无法获取 Binance 收益流水以发现交易币种：{exc}") from exc
            if len(income or []) >= 1000:
                raise ReconcileError("Binance 单日收益流水达到 1000 条，无法证明币种集合完整")
            symbols.update(
                normalize_symbol(row.get("symbol"))
                for row in (income or [])
                if row.get("symbol")
            )
            cursor = window_end + 1
        try:
            positions = await self.client.raw.fetch_positions()
            symbols.update(
                normalize_symbol((row.get("info") or {}).get("symbol") or row.get("symbol"))
                for row in positions
                if abs(float(row.get("contracts") or (row.get("info") or {}).get("positionAmt") or 0)) > 0
            )
        except Exception as exc:
            raise ReconcileError(f"无法获取 Binance 当前持仓：{exc}") from exc
        return sorted(symbol for symbol in symbols if symbol)

    async def _local_fills(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        async with self.store._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(ExchangeFillRow)
                    .where(ExchangeFillRow.ts_ms >= start_ms)
                    .where(ExchangeFillRow.ts_ms <= end_ms)
                    .order_by(ExchangeFillRow.symbol, ExchangeFillRow.ts_ms, ExchangeFillRow.id)
                )
            ).scalars().all()
        return [
            {
                "id": row.id,
                "ts_ms": row.ts_ms,
                "symbol": row.symbol,
                "exchange_trade_id": row.exchange_trade_id,
                "exchange_order_id": row.exchange_order_id,
                "client_order_id": row.client_order_id,
                "side": row.side,
                "qty": row.qty,
                "price": row.price,
                "fee": row.fee,
                "fee_asset": row.fee_asset,
                "realized_pnl": row.realized_pnl,
                "liquidity": row.liquidity,
                "reduce_only": row.reduce_only,
                "ownership": row.ownership,
                "resolved_client_order_id": row.resolved_client_order_id,
                "resolved_reduce_only": row.resolved_reduce_only,
                "resolved_order_type": row.resolved_order_type,
                "resolved_algo_id": row.resolved_algo_id,
                "resolved_metadata_source": row.resolved_metadata_source,
            }
            for row in rows
        ]

    async def _remote_fills(
        self, symbols: list[str], start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        total_symbols = max(len(symbols), 1)
        for symbol_index, symbol in enumerate(symbols, start=1):
            await self._progress(
                "remote_fills",
                25 + int(18 * (symbol_index - 1) / total_symbols),
                f"拉取 {symbol} 成交（{symbol_index}/{len(symbols)}）",
            )
            cursor = start_ms
            while cursor <= end_ms:
                window_end = min(cursor + WINDOW_MS - 1, end_ms)
                try:
                    trades = await self.client.fetch_my_trades(
                        symbol, since=cursor, until=window_end, limit=1000
                    )
                except Exception as exc:
                    raise ReconcileError(f"拉取 {symbol} Binance 成交失败：{exc}") from exc
                if len(trades) >= 1000:
                    raise ReconcileError(
                        f"{symbol} 在单个六日窗口达到 1000 笔，无法证明数据完整"
                    )
                for trade in trades:
                    fill = ccxt_trade_fill(trade, symbol)
                    if fill and start_ms <= int(fill["ts_ms"]) <= end_ms:
                        rows[(fill["symbol"], fill["exchange_trade_id"])] = fill
                cursor = window_end + 1
        return sorted(rows.values(), key=lambda row: (
            row["symbol"], int(row["ts_ms"]), int(row["exchange_trade_id"])
        ))

    @staticmethod
    def _verify_remote_local(
        local_rows: list[dict[str, Any]], remote_rows: list[dict[str, Any]]
    ) -> None:
        local = {(row["symbol"], row["exchange_trade_id"]): row for row in local_rows}
        remote = {(row["symbol"], row["exchange_trade_id"]): row for row in remote_rows}
        missing = sorted(set(remote) - set(local))
        extra = sorted(set(local) - set(remote))
        if missing or extra:
            raise ReconcileError(
                f"本地与 Binance 成交集合不一致：本地缺失 {len(missing)}，本地多出 {len(extra)}"
            )
        mismatches: list[str] = []
        for key in sorted(local):
            left, right = local[key], remote[key]
            if (
                str(left["exchange_order_id"]) != str(right["exchange_order_id"])
                or str(left["side"]).lower() != str(right["side"]).lower()
                or not _almost_equal(left["qty"], right["qty"])
                or not _almost_equal(left["price"], right["price"])
                or not _almost_equal(left["fee"], right["fee"])
                or not _almost_equal(left["realized_pnl"], right["realized_pnl"])
            ):
                mismatches.append(f"{key[0]}:{key[1]}")
        if mismatches:
            raise ReconcileError(f"成交字段与 Binance 不一致：{', '.join(mismatches[:10])}")

    async def _fetch_order_metadata(
        self,
        local_rows: list[dict[str, Any]],
        event_metadata: dict[str, dict[str, Any]],
        cached_metadata: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        keys = sorted({
            (row["symbol"], str(row["exchange_order_id"]))
            for row in local_rows
            if row["exchange_order_id"]
            and (row["symbol"], str(row["exchange_order_id"])) not in cached_metadata
            and not self._event_metadata_complete(
                event_metadata.get(str(row["exchange_order_id"]), {})
            )
        })
        semaphore = asyncio.Semaphore(6)

        async def fetch(key: tuple[str, str]):
            symbol, order_id = key
            async with semaphore:
                try:
                    raw = await self.client.raw.fapiPrivateGetOrder({
                        "symbol": symbol,
                        "orderId": order_id,
                    })
                except Exception as exc:
                    raise ReconcileError(f"查询 Binance 订单 {symbol}:{order_id} 失败：{exc}") from exc
                return key, dict(raw or {})

        if not keys:
            await self._progress("order_metadata", 72, "复用本地已核对订单元数据")
            return {}

        completed = 0
        output: dict[tuple[str, str], dict[str, Any]] = {}

        async def fetch_with_progress(key: tuple[str, str]):
            nonlocal completed
            item = await fetch(key)
            completed += 1
            await self._progress(
                "order_metadata",
                55 + int(17 * completed / len(keys)),
                f"查询 Binance 订单元数据（{completed}/{len(keys)}）",
            )
            return item

        output.update(await asyncio.gather(*(fetch_with_progress(key) for key in keys)))
        return output

    @staticmethod
    def _event_metadata_complete(metadata: dict[str, Any]) -> bool:
        return bool(
            metadata.get("client_order_id")
            and metadata.get("order_type")
            and "reduce_only" in metadata
        )

    @staticmethod
    def _cached_order_metadata(
        local_rows: list[dict[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        cached: dict[tuple[str, str], dict[str, Any]] = {}
        for row in local_rows:
            key = (row["symbol"], str(row.get("exchange_order_id") or ""))
            if not key[1] or not row.get("resolved_metadata_source"):
                continue
            if not row.get("resolved_client_order_id"):
                continue
            cached[key] = {
                "clientOrderId": row["resolved_client_order_id"],
                "reduceOnly": bool(row.get("resolved_reduce_only")),
                "origType": row.get("resolved_order_type") or "",
                "_cached_algo_id": row.get("resolved_algo_id") or "",
                "_metadata_source": row.get("resolved_metadata_source") or "local_reconciled",
            }
        return cached

    async def _event_order_metadata(
        self, start_ms: int, end_ms: int
    ) -> dict[str, dict[str, Any]]:
        async with self.store._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(ExchangeEventRow)
                    .where(ExchangeEventRow.event_time_ms >= start_ms - 24 * 60 * 60 * 1000)
                    .where(ExchangeEventRow.event_time_ms <= end_ms)
                    .where(ExchangeEventRow.event_type.in_(("ALGO_UPDATE", "ORDER_TRADE_UPDATE")))
                    .order_by(ExchangeEventRow.id)
                )
            ).scalars().all()
        algo: dict[str, dict[str, Any]] = {}
        actual: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row.raw_json or "{}")
            except json.JSONDecodeError:
                continue
            order = payload.get("o") or {}
            if row.event_type == "ALGO_UPDATE":
                algo_id = str(order.get("aid") or order.get("algoId") or "")
                if algo_id:
                    algo[algo_id] = {
                        "algo_id": algo_id,
                        "order_type": str(order.get("o") or order.get("orderType") or "").upper(),
                        "reduce_only": _bool(order.get("R") or order.get("reduceOnly")),
                        "client_order_id": str(order.get("caid") or order.get("clientAlgoId") or ""),
                    }
                actual_id = str(order.get("ai") or order.get("actualOrderId") or "")
                if actual_id and algo_id:
                    actual[actual_id] = dict(algo[algo_id])
            else:
                actual_id = str(order.get("i") or "")
                algo_id = str(order.get("si") or "")
                if actual_id and algo_id:
                    actual[actual_id] = {
                        **algo.get(algo_id, {}),
                        "algo_id": algo_id,
                        "client_order_id": str(order.get("c") or ""),
                        "reduce_only": _bool(order.get("R")),
                    }
        for actual_id, meta in list(actual.items()):
            if meta.get("algo_id") in algo:
                actual[actual_id] = {**algo[meta["algo_id"]], **meta}
        return actual

    @staticmethod
    def _resolve_fills(
        local_rows: list[dict[str, Any]],
        orders: dict[tuple[str, str], dict[str, Any]],
        event_metadata: dict[str, dict[str, Any]],
        cached_metadata: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        resolved = []
        cached_metadata = cached_metadata or {}
        for local in local_rows:
            key = (local["symbol"], str(local["exchange_order_id"]))
            order = orders.get(key) or cached_metadata.get(key, {})
            event = event_metadata.get(str(local["exchange_order_id"]), {})
            client_id = str(
                order.get("clientOrderId") or event.get("client_order_id")
                or local.get("client_order_id") or ""
            )
            ownership = "engine" if client_id.startswith("bt-") else "external"
            reduce_only = _bool(
                order.get("reduceOnly")
                if order.get("reduceOnly") is not None
                else event.get("reduce_only", local.get("reduce_only"))
            )
            order_type = str(
                event.get("order_type") or order.get("origType") or order.get("type") or ""
            ).upper()
            if order_type.startswith("TAKE_PROFIT"):
                exit_reason = "TP"
            elif order_type.startswith("STOP") or order_type == "TRAILING_STOP_MARKET":
                exit_reason = "SL"
            elif reduce_only:
                exit_reason = "CLOSE" if ownership == "engine" else "MANUAL_REDUCE"
            else:
                exit_reason = "CLOSE" if ownership == "engine" else "MANUAL_CLOSE"
            resolved.append({
                "exchange_fill_id": int(local["id"]),
                "ts_ms": int(local["ts_ms"]),
                "symbol": local["symbol"],
                "exchange_trade_id": local["exchange_trade_id"],
                "exchange_order_id": local["exchange_order_id"],
                "client_order_id": client_id,
                "side": local["side"],
                "qty": float(local["qty"]),
                "price": float(local["price"]),
                "fee": float(local["fee"]),
                "fee_asset": local["fee_asset"],
                "realized_pnl": float(local["realized_pnl"]),
                "liquidity": local["liquidity"],
                "reduce_only": reduce_only,
                "ownership": ownership,
                "order_type": order_type,
                "exit_reason": exit_reason,
                "algo_id": str(
                    event.get("algo_id") or order.get("_cached_algo_id") or ""
                ),
                "metadata_source": str(
                    order.get("_metadata_source")
                    or ("binance_order+private_event" if event else "binance_order")
                ),
                "original_ownership": local["ownership"],
                "original_client_order_id": local["client_order_id"],
                "original_reduce_only": bool(local["reduce_only"]),
            })
        return resolved

    async def _current_positions(self, symbols: list[str]) -> dict[str, float]:
        try:
            raw_positions = await self.client.fetch_positions(symbols)
        except Exception as exc:
            raise ReconcileError(f"获取当前持仓失败：{exc}") from exc
        positions = {symbol: 0.0 for symbol in symbols}
        for raw in raw_positions:
            position = normalize_position(raw)
            qty = float(position.get("contracts") or 0)
            if position.get("side") == "short":
                qty = -qty
            positions[normalize_symbol(position.get("symbol"))] = qty
        return positions

    @staticmethod
    def _derive_initial_positions(
        rows: list[dict[str, Any]], current: dict[str, float]
    ) -> dict[str, float]:
        net: dict[str, float] = defaultdict(float)
        for row in rows:
            net[row["symbol"]] += row["qty"] if row["side"] == "buy" else -row["qty"]
        return {
            symbol: float(current.get(symbol, 0)) - float(net.get(symbol, 0))
            for symbol in set(current) | set(net)
        }

    @staticmethod
    def _validate_final_positions(rebuilt, current: dict[str, float]) -> list[str]:
        errors = []
        for symbol in sorted(set(rebuilt) | set(current)):
            left = float(rebuilt.get(symbol, 0))
            right = float(current.get(symbol, 0))
            if abs(left - right) > float(EPSILON):
                errors.append(f"{symbol} 重建仓位 {left} != Binance {right}")
        return errors

    async def _persist_preview(
        self, result: dict[str, Any], *, scope_start_ms: int, scope_end_ms: int
    ) -> int:
        async with self.store._sessionmaker() as session:
            existing = (
                await session.execute(
                    select(ExchangeReconcileRunRow)
                    .where(ExchangeReconcileRunRow.preview_hash == result["preview_hash"])
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id
            summary = result["summary"]
            run = ExchangeReconcileRunRow(
                scope_start_ms=scope_start_ms,
                scope_end_ms=scope_end_ms,
                preview_hash=result["preview_hash"],
                local_fill_count=summary["fills"]["local"],
                remote_fill_count=summary["fills"]["remote"],
                cycle_count=summary["cycles"]["after"],
                ownership_change_count=summary["fills"]["ownership_changes"],
                metadata_change_count=summary["fills"]["metadata_changes"],
                summary_json=_canonical_json(summary),
            )
            session.add(run)
            await session.flush()
            fills_by_id = {
                row["exchange_fill_id"]: row for row in result["_resolved_fills"]
            }
            for cycle in result["cycles"]:
                cycle_row = BinanceTradeCycleRow(
                    run_id=run.id,
                    sequence=cycle["sequence"],
                    symbol=cycle["symbol"],
                    direction=cycle["direction"],
                    ownership=cycle["ownership"],
                    status=cycle["status"],
                    opened_at_ms=cycle["opened_at_ms"],
                    opened_at=_iso(cycle["opened_at_ms"]),
                    closed_at_ms=cycle["closed_at_ms"],
                    closed_at=_iso(cycle["closed_at_ms"]),
                    entry_price=cycle["entry_price"],
                    exit_price=cycle["exit_price"],
                    qty_opened=cycle["qty_opened"],
                    qty_closed=cycle["qty_closed"],
                    entry_notional=cycle["entry_notional"],
                    exit_notional=cycle["exit_notional"],
                    entry_fee=cycle["entry_fee"],
                    exit_fee=cycle["exit_fee"],
                    total_fee=cycle["total_fee"],
                    gross_realized_pnl=cycle["gross_realized_pnl"],
                    net_realized_pnl=cycle["net_realized_pnl"],
                    entry_liquidity=cycle["entry_liquidity"],
                    exit_liquidity=cycle["exit_liquidity"],
                    exit_reason=cycle["exit_reason"],
                    confidence=cycle["confidence"],
                    classification_reason=cycle["classification_reason"],
                )
                session.add(cycle_row)
                await session.flush()
                for allocation in cycle["allocations"]:
                    if allocation["exchange_fill_id"] not in fills_by_id:
                        raise ReconcileError("周期引用了范围外成交")
                    session.add(BinanceTradeCycleFillRow(
                        run_id=run.id,
                        cycle_id=cycle_row.id,
                        exchange_fill_id=allocation["exchange_fill_id"],
                        role=allocation["role"],
                        qty=allocation["qty"],
                        price=allocation["price"],
                        fee=allocation["fee"],
                        realized_pnl=allocation["realized_pnl"],
                        fill_ownership=allocation["fill_ownership"],
                        exit_reason=allocation["exit_reason"],
                    ))
            await session.commit()
            return run.id

    async def _load_run(self, run_id: int) -> ExchangeReconcileRunRow | None:
        async with self.store._sessionmaker() as session:
            return await session.get(ExchangeReconcileRunRow, int(run_id))

    async def _activate_run(
        self, run_id: int, resolved_fills: list[dict[str, Any]]
    ) -> None:
        now_ms = int(time.time() * 1000)
        now = _iso(now_ms)
        async with self.store._sessionmaker() as session:
            run = await session.get(ExchangeReconcileRunRow, run_id)
            if run is None:
                raise ReconcileError("核对运行不存在")
            for item in resolved_fills:
                fill = await session.get(ExchangeFillRow, item["exchange_fill_id"])
                if fill is None:
                    raise ReconcileError(f"成交 {item['exchange_fill_id']} 已被删除")
                fill.resolved_ownership = item["ownership"]
                fill.resolved_client_order_id = item["client_order_id"]
                fill.resolved_reduce_only = item["reduce_only"]
                fill.resolved_order_type = item["order_type"]
                fill.resolved_exit_reason = item["exit_reason"]
                fill.resolved_algo_id = item["algo_id"]
                fill.resolved_metadata_source = item["metadata_source"]
                fill.reconciled_at_ms = now_ms
            previous = (
                await session.execute(
                    select(ExchangeReconcileRunRow)
                    .where(ExchangeReconcileRunRow.status == "applied")
                    .where(ExchangeReconcileRunRow.id != run_id)
                )
            ).scalars().all()
            for row in previous:
                row.status = "superseded"
            run.status = "applied"
            run.applied_at_ms = now_ms
            setting = await session.get(RuntimeSettingRow, ACTIVE_RUN_SETTING)
            if setting is None:
                session.add(RuntimeSettingRow(
                    key=ACTIVE_RUN_SETTING, value=str(run_id), updated_at=now
                ))
            else:
                setting.value = str(run_id)
                setting.updated_at = now
            await session.commit()

    def _backup_database(self) -> str:
        source = Path(self.db_path).resolve()
        backup_dir = source.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / (
            f"{source.stem}-before-binance-reconcile-"
            f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}{source.suffix}"
        )
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
        shutil.copymode(source, target)
        return str(target)

    async def _scalar(self, sql: str) -> Any:
        async with self.store._sessionmaker() as session:
            return (await session.execute(text(sql))).scalar_one()
